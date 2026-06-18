use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::sqlite::Database;

const JSON_SUFFIXES: &[&str] = &["jsonl", "json", "log"];
const SQLITE_SUFFIXES: &[&str] = &["sqlite", "sqlite3", "db"];

#[derive(Debug, Default)]
pub struct AuditReport {
    pub files: usize,
    pub sqlite_files: usize,
    pub json_files: usize,
    pub tables: BTreeMap<String, TableReport>,
    pub event_hints: BTreeMap<String, usize>,
    pub fields: BTreeMap<String, usize>,
    pub errors: Vec<String>,
    pub coverage: Coverage,
}

#[derive(Debug, Default)]
#[allow(dead_code)]
pub struct TableReport {
    pub rows: usize,
    pub columns: Vec<String>,
}

#[derive(Debug, Default)]
pub struct Coverage {
    pub has_prompt: bool,
    pub has_tool: bool,
    pub has_usage: bool,
    pub has_timestamps: bool,
    pub has_sessions: bool,
}

pub fn audit_codex_paths(paths: &[String]) -> AuditReport {
    let mut report = AuditReport::default();
    for raw_path in paths {
        let path = expand_path(Path::new(raw_path));
        if !path.exists() {
            continue;
        }
        let files = match iter_ingestable_files(&path) {
            Ok(files) => files,
            Err(err) => {
                report.errors.push(format!("{}: {err}", path.display()));
                continue;
            }
        };
        for file_path in files {
            report.files += 1;
            if has_suffix(&file_path, SQLITE_SUFFIXES) {
                report.sqlite_files += 1;
                audit_sqlite_file(&file_path, &mut report);
            } else {
                report.json_files += 1;
                audit_json_file(&file_path, &mut report);
            }
        }
    }
    report.coverage = coverage_summary(&report);
    report
}

fn audit_sqlite_file(path: &Path, report: &mut AuditReport) {
    let source = match Database::open(path) {
        Ok(source) => source,
        Err(err) => {
            report.errors.push(format!("{}: {err}", path.display()));
            return;
        }
    };
    let tables = match source.query(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
        &[],
    ) {
        Ok(tables) => tables,
        Err(err) => {
            report.errors.push(format!("{}: {err}", path.display()));
            return;
        }
    };
    for table_row in tables {
        let Some(table) = table_row.get("name").filter(|value| !value.is_empty()) else {
            continue;
        };
        let quoted = quote_identifier(table);
        let count = source
            .query(&format!("SELECT COUNT(*) AS count FROM {quoted}"), &[])
            .ok()
            .and_then(|rows| {
                rows.first()
                    .and_then(|row| row.get("count"))
                    .and_then(|value| value.parse::<usize>().ok())
            })
            .unwrap_or(0);
        let columns = source
            .query(&format!("PRAGMA table_info({quoted})"), &[])
            .unwrap_or_default()
            .into_iter()
            .filter_map(|row| row.get("name").cloned())
            .collect::<Vec<_>>();
        report.tables.insert(
            format!(
                "{}:{table}",
                path.file_name()
                    .map(|name| name.to_string_lossy())
                    .unwrap_or_default()
            ),
            TableReport {
                rows: count,
                columns,
            },
        );
        let rows = match source.query(&format!("SELECT * FROM {quoted} LIMIT 100"), &[]) {
            Ok(rows) => rows,
            Err(err) => {
                report
                    .errors
                    .push(format!("{}:{table}: {err}", path.display()));
                continue;
            }
        };
        for row in rows {
            observe_payload(sqlite_row_payload(row), report);
        }
    }
}

fn audit_json_file(path: &Path, report: &mut AuditReport) {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(err) => {
            report.errors.push(format!("{}: {err}", path.display()));
            return;
        }
    };
    for line in text.lines().take(100) {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if let Ok(Value::Object(payload)) = serde_json::from_str::<Value>(line) {
            observe_payload(payload, report);
        }
    }
}

fn sqlite_row_payload(row: BTreeMap<String, String>) -> serde_json::Map<String, Value> {
    let mut payload = serde_json::Map::new();
    for (key, value) in row {
        if let Some(Value::Object(object)) = parse_possible_json(&value) {
            for (key, value) in object {
                payload.insert(key, value);
            }
        } else if !value.is_empty() {
            payload.insert(key, Value::String(value));
        }
    }
    payload
}

fn observe_payload(payload: serde_json::Map<String, Value>, report: &mut AuditReport) {
    if payload.is_empty() {
        return;
    }
    let hint = event_hint(&payload);
    *report.event_hints.entry(hint).or_default() += 1;
    for field in payload.keys() {
        *report.fields.entry(field.clone()).or_default() += 1;
    }
}

fn event_hint(payload: &serde_json::Map<String, Value>) -> String {
    if let Some(value) = payload
        .get("event_type")
        .or_else(|| payload.get("hook_event_name"))
        .or_else(|| payload.get("type"))
        .and_then(Value::as_str)
    {
        return value.to_string();
    }
    if payload.get("tool_name").is_some() || payload.get("tool").is_some() {
        return "tool".to_string();
    }
    if payload.get("prompt").is_some() {
        return "prompt".to_string();
    }
    if let Some(role) = payload
        .get("message")
        .and_then(|message| message.get("role"))
        .and_then(Value::as_str)
    {
        return role.to_string();
    }
    "unknown".to_string()
}

fn coverage_summary(report: &AuditReport) -> Coverage {
    let fields = report.fields.keys().cloned().collect::<BTreeSet<_>>();
    let hints = report
        .event_hints
        .keys()
        .map(|key| key.to_ascii_lowercase())
        .collect::<BTreeSet<_>>();
    Coverage {
        has_prompt: ["prompt", "user_prompt", "input"]
            .iter()
            .any(|field| fields.contains(*field))
            || ["user", "prompt"].iter().any(|hint| hints.contains(*hint)),
        has_tool: ["tool", "tool_name", "toolName"]
            .iter()
            .any(|field| fields.contains(*field))
            || hints.contains("tool"),
        has_usage: [
            "usage",
            "cost_usd",
            "cost_usd_est",
            "total_cost_usd",
            "input_tokens",
            "output_tokens",
        ]
        .iter()
        .any(|field| fields.contains(*field)),
        has_timestamps: ["ts", "timestamp", "created_at", "time"]
            .iter()
            .any(|field| fields.contains(*field)),
        has_sessions: ["session_id", "sessionId", "conversation_id", "rollout_id"]
            .iter()
            .any(|field| fields.contains(*field)),
    }
}

fn iter_ingestable_files(path: &Path) -> Result<Vec<PathBuf>, String> {
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

fn parse_possible_json(text: &str) -> Option<Value> {
    let stripped = text.trim();
    if !stripped.starts_with('{') && !stripped.starts_with('[') {
        return None;
    }
    serde_json::from_str(stripped).ok()
}

fn has_suffix(path: &Path, suffixes: &[&str]) -> bool {
    path.extension()
        .and_then(|value| value.to_str())
        .is_some_and(|suffix| {
            let suffix = suffix.to_ascii_lowercase();
            suffixes.contains(&suffix.as_str())
        })
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
    use crate::sqlite::{Bind, Database};
    use serde_json::json;

    #[test]
    fn reports_rollout_coverage() {
        let root =
            std::env::temp_dir().join(format!("tranquil-rust-audit-{}", crate::util::now_millis()));
        std::fs::create_dir_all(&root).unwrap();
        let rollout = root.join("codex.db");
        let db = Database::open(&rollout).unwrap();
        db.execute("CREATE TABLE items (payload TEXT)", &[])
            .unwrap();
        db.execute(
            "INSERT INTO items (payload) VALUES (?)",
            &[Bind::Text(
                serde_json::to_string(&json!({
                    "session_id": "cx",
                    "timestamp": "2026-06-17T12:00:00Z",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pytest"},
                    "usage": {"input_tokens": 1, "output_tokens": 2, "cost_usd": 0.01}
                }))
                .unwrap(),
            )],
        )
        .unwrap();
        drop(db);
        let report = audit_codex_paths(&[rollout.display().to_string()]);
        assert_eq!(report.files, 1);
        assert_eq!(report.sqlite_files, 1);
        assert!(report.coverage.has_tool);
        assert!(report.coverage.has_usage);
        assert!(report.coverage.has_sessions);
        let _ = std::fs::remove_dir_all(root);
    }
}
