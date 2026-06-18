use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use serde_json::{Value, json};

use crate::config::SignalThresholds;
use crate::normalize::normalize_event;
use crate::sqlite::{Bind, Database};
use crate::storage::Storage;

const JSON_SUFFIXES: &[&str] = &["jsonl", "json", "log"];
const SQLITE_SUFFIXES: &[&str] = &["sqlite", "sqlite3", "db"];

#[derive(Debug, Clone)]
pub struct SqliteTable {
    pub name: String,
    pub text_columns: Vec<String>,
    pub row_count: usize,
}

pub fn ingest_path(
    storage: &Storage,
    path: &Path,
    agent: Option<&str>,
    limit: Option<usize>,
    thresholds: &SignalThresholds,
) -> Result<usize, String> {
    let target = expand_path(path);
    if !target.exists() {
        return Err(format!("path does not exist: {}", target.display()));
    }
    let mut count = 0;
    for file_path in iter_ingestable_files(&target)? {
        if limit.is_some_and(|limit| count >= limit) {
            break;
        }
        let remaining = limit.map(|limit| limit.saturating_sub(count));
        let added = if has_suffix(&file_path, SQLITE_SUFFIXES) {
            ingest_sqlite(storage, &file_path, agent, remaining, thresholds)?
        } else {
            ingest_json_lines(storage, &file_path, agent, remaining, thresholds)?
        };
        count += added;
    }
    Ok(count)
}

pub fn iter_ingestable_files(path: &Path) -> Result<Vec<PathBuf>, String> {
    if path.is_file() {
        return Ok(vec![path.to_path_buf()]);
    }
    let mut files = Vec::new();
    collect_ingestable_files(path, &mut files)?;
    files.sort();
    Ok(files)
}

fn collect_ingestable_files(path: &Path, files: &mut Vec<PathBuf>) -> Result<(), String> {
    for entry in std::fs::read_dir(path).map_err(|err| err.to_string())? {
        let entry = entry.map_err(|err| err.to_string())?;
        let child = entry.path();
        if child.is_dir() {
            collect_ingestable_files(&child, files)?;
        } else if child.is_file()
            && (has_suffix(&child, JSON_SUFFIXES) || has_suffix(&child, SQLITE_SUFFIXES))
        {
            files.push(child);
        }
    }
    Ok(())
}

fn ingest_json_lines(
    storage: &Storage,
    path: &Path,
    agent: Option<&str>,
    limit: Option<usize>,
    thresholds: &SignalThresholds,
) -> Result<usize, String> {
    ingest_json_lines_from_offset(storage, path, agent, limit, 0, thresholds)
        .map(|(count, _offset)| count)
}

pub fn ingest_json_lines_from_offset(
    storage: &Storage,
    path: &Path,
    agent: Option<&str>,
    limit: Option<usize>,
    start_offset: u64,
    thresholds: &SignalThresholds,
) -> Result<(usize, u64), String> {
    let file = File::open(path).map_err(|err| err.to_string())?;
    let mut reader = BufReader::new(file);
    if start_offset > 0 {
        reader
            .seek(SeekFrom::Start(start_offset))
            .map_err(|err| err.to_string())?;
    }
    let fallback_ts = file_timestamp(path);
    let mut count = 0;
    let mut line = String::new();
    loop {
        if limit.is_some_and(|limit| count >= limit) {
            break;
        }
        line.clear();
        let bytes = reader.read_line(&mut line).map_err(|err| err.to_string())?;
        if bytes == 0 {
            break;
        }
        let line = line.trim().trim_start_matches('\u{feff}');
        if line.is_empty() {
            continue;
        }
        let Ok(Value::Object(mut payload)) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if let Some(agent) = agent {
            payload
                .entry("agent".to_string())
                .or_insert_with(|| Value::String(agent.to_string()));
        }
        if !has_any_key(&payload, &["ts", "timestamp", "created_at", "time"]) {
            payload.insert("timestamp".to_string(), json!(fallback_ts));
        }
        let payload = Value::Object(payload);
        let event = normalize_event(&infer_event_hint(&payload), &payload, "transcript");
        if record_if_new(storage, &event, thresholds)? {
            count += 1;
        }
    }
    let offset = reader.stream_position().map_err(|err| err.to_string())?;
    Ok((count, offset))
}

fn ingest_sqlite(
    storage: &Storage,
    path: &Path,
    agent: Option<&str>,
    limit: Option<usize>,
    thresholds: &SignalThresholds,
) -> Result<usize, String> {
    let mut count = 0;
    for table in sqlite_tables(path)? {
        if limit.is_some_and(|limit| count >= limit) {
            break;
        }
        let (added, _) = ingest_sqlite_table_from_offset(
            storage,
            path,
            &table,
            0,
            agent,
            limit.map(|limit| limit.saturating_sub(count)),
            thresholds,
        )?;
        count += added;
    }
    Ok(count)
}

pub fn sqlite_tables(path: &Path) -> Result<Vec<SqliteTable>, String> {
    let source = Database::open(path)?;
    let tables = source.query(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
        &[],
    )?;
    let mut result = Vec::new();
    for row in tables {
        let Some(table) = row.get("name").filter(|value| !value.is_empty()) else {
            continue;
        };
        let quoted = quote_identifier(table);
        let columns = source.query(&format!("PRAGMA table_info({quoted})"), &[])?;
        let text_columns = columns
            .iter()
            .filter_map(|row| {
                let ty = row.get("type").map(|value| value.to_ascii_uppercase());
                if ty
                    .as_deref()
                    .is_some_and(|value| matches!(value, "" | "TEXT" | "JSON" | "BLOB"))
                {
                    row.get("name").cloned()
                } else {
                    None
                }
            })
            .collect::<Vec<_>>();
        let row_count = source
            .query(&format!("SELECT COUNT(*) AS count FROM {quoted}"), &[])?
            .first()
            .and_then(|row| row.get("count"))
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(0);
        result.push(SqliteTable {
            name: table.to_string(),
            text_columns,
            row_count,
        });
    }
    Ok(result)
}

pub fn ingest_sqlite_table_from_offset(
    storage: &Storage,
    path: &Path,
    table: &SqliteTable,
    offset: usize,
    agent: Option<&str>,
    limit: Option<usize>,
    thresholds: &SignalThresholds,
) -> Result<(usize, usize), String> {
    if table.text_columns.is_empty() {
        return Ok((0, table.row_count));
    }
    let source = Database::open(path)?;
    let rows = source.query(
        &format!(
            "SELECT * FROM {} LIMIT -1 OFFSET ?",
            quote_identifier(&table.name)
        ),
        &[Bind::Int(offset as i64)],
    )?;
    let mut count = 0;
    for row in rows {
        if limit.is_some_and(|limit| count >= limit) {
            break;
        }
        let Some(mut payload) = row_to_payload(row, &table.text_columns) else {
            continue;
        };
        payload
            .entry("agent".to_string())
            .or_insert_with(|| Value::String(agent.unwrap_or("codex").to_string()));
        payload
            .entry("rollout_table".to_string())
            .or_insert_with(|| Value::String(table.name.clone()));
        let payload = Value::Object(payload);
        let event = normalize_event(&infer_event_hint(&payload), &payload, "transcript");
        if record_if_new(storage, &event, thresholds)? {
            count += 1;
        }
    }
    Ok((count, table.row_count))
}

fn row_to_payload(
    row: BTreeMap<String, String>,
    text_columns: &[String],
) -> Option<serde_json::Map<String, Value>> {
    let mut merged = serde_json::Map::new();
    for (column, value) in row {
        if value.is_empty() {
            continue;
        }
        if text_columns.contains(&column) {
            if let Some(Value::Object(object)) = parse_possible_json(&value) {
                for (key, value) in object {
                    merged.insert(key, value);
                }
            } else if matches!(
                column.to_ascii_lowercase().as_str(),
                "message" | "content" | "text" | "prompt"
            ) {
                merged.insert(column, Value::String(value));
            }
        } else if let Ok(number) = value.parse::<i64>() {
            merged.insert(column, json!(number));
        } else if let Ok(number) = value.parse::<f64>() {
            merged.insert(column, json!(number));
        } else {
            merged.insert(column, Value::String(value));
        }
    }
    if merged.is_empty() {
        None
    } else {
        Some(merged)
    }
}

fn record_if_new(
    storage: &Storage,
    event: &Value,
    thresholds: &SignalThresholds,
) -> Result<bool, String> {
    let event_id = event
        .get("event_id")
        .and_then(Value::as_str)
        .ok_or_else(|| "normalized event missing event_id".to_string())?;
    let existed = storage.event_exists(event_id)?;
    storage.record_event_with_thresholds(event, thresholds)?;
    Ok(!existed)
}

fn parse_possible_json(text: &str) -> Option<Value> {
    let stripped = text.trim();
    if !stripped.starts_with('{') && !stripped.starts_with('[') {
        return None;
    }
    serde_json::from_str(stripped).ok()
}

fn infer_event_hint(payload: &Value) -> String {
    if let Some(explicit) = payload
        .get("event_type")
        .or_else(|| payload.get("hook_event_name"))
        .or_else(|| payload.get("type"))
        .and_then(Value::as_str)
    {
        let role = explicit.to_ascii_lowercase();
        if matches!(role.as_str(), "user" | "user_message") {
            return "user-prompt-submit".to_string();
        }
        if matches!(role.as_str(), "assistant" | "assistant_message") {
            return "stop".to_string();
        }
        return explicit.to_string();
    }
    if let Some(role) = payload
        .get("message")
        .and_then(|message| message.get("role"))
        .and_then(Value::as_str)
    {
        if role.eq_ignore_ascii_case("user") {
            return "user-prompt-submit".to_string();
        }
        if role.eq_ignore_ascii_case("assistant") {
            return "stop".to_string();
        }
    }
    if payload.get("prompt").is_some() {
        return "user-prompt-submit".to_string();
    }
    if payload.get("tool_name").is_some() || payload.get("tool").is_some() {
        return "post-tool-use".to_string();
    }
    "event".to_string()
}

fn has_any_key(map: &serde_json::Map<String, Value>, keys: &[&str]) -> bool {
    keys.iter().any(|key| map.contains_key(*key))
}

pub fn is_json_file(path: &Path) -> bool {
    has_suffix(path, JSON_SUFFIXES)
}

pub fn is_sqlite_file(path: &Path) -> bool {
    has_suffix(path, SQLITE_SUFFIXES)
}

fn has_suffix(path: &Path, suffixes: &[&str]) -> bool {
    path.extension()
        .and_then(|value| value.to_str())
        .is_some_and(|suffix| {
            let suffix = suffix.to_ascii_lowercase();
            suffixes.contains(&suffix.as_str())
        })
}

fn file_timestamp(path: &Path) -> f64 {
    path.metadata()
        .and_then(|metadata| metadata.modified())
        .ok()
        .and_then(|modified| modified.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn expand_path(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    if text == "~" {
        return PathBuf::from(home_dir());
    }
    if let Some(rest) = text.strip_prefix("~/").or_else(|| text.strip_prefix("~\\")) {
        return PathBuf::from(home_dir()).join(rest);
    }
    path.to_path_buf()
}

fn home_dir() -> String {
    std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_else(|_| ".".to_string())
}

fn quote_identifier(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\"\""))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sqlite::Bind;

    #[test]
    fn jsonl_ingest_backfills_events() {
        let root = std::env::temp_dir().join(format!(
            "tranquil-rust-ingest-{}",
            crate::util::now_millis()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let transcript = root.join("session.jsonl");
        std::fs::write(
            &transcript,
            format!(
                "\u{feff}{}\n{}\n",
                json!({"type": "user", "session_id": "jsonl", "prompt": "build it"}),
                json!({"session_id": "jsonl", "tool_name": "Bash", "tool_input": {"command": "pytest"}})
            ),
        )
        .unwrap();
        let storage = Storage::open(&root.join("tranquil.db")).unwrap();
        let count = ingest_path(
            &storage,
            &transcript,
            None,
            None,
            &SignalThresholds::default(),
        )
        .unwrap();
        assert_eq!(count, 2);
        let runs = storage.list_runs(10).unwrap();
        assert_eq!(runs.len(), 1);
        assert_eq!(runs[0].first_prompt.as_deref(), Some("build it"));
        assert!(runs[0].checks_ran);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn sqlite_ingest_imports_generic_payload_rows() {
        let root = std::env::temp_dir().join(format!(
            "tranquil-rust-ingest-sqlite-{}",
            crate::util::now_millis()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let source_path = root.join("codex.db");
        let source = Database::open(&source_path).unwrap();
        source
            .execute("CREATE TABLE rollouts (payload TEXT)", &[])
            .unwrap();
        source
            .execute(
                "INSERT INTO rollouts (payload) VALUES (?)",
                &[Bind::Text(
                    serde_json::to_string(&json!({
                        "agent": "codex",
                        "session_id": "rollout",
                        "timestamp": "2026-06-17T12:00:00Z",
                        "prompt": "tail me"
                    }))
                    .unwrap(),
                )],
            )
            .unwrap();
        drop(source);
        let storage = Storage::open(&root.join("tranquil.db")).unwrap();
        let count = ingest_path(
            &storage,
            &source_path,
            Some("codex"),
            None,
            &SignalThresholds::default(),
        )
        .unwrap();
        assert_eq!(count, 1);
        assert_eq!(storage.list_runs(10).unwrap()[0].agent, "codex");
        let _ = std::fs::remove_dir_all(root);
    }
}
