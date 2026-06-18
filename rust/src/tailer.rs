use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::config::Config;
use crate::ingest::{
    ingest_json_lines_from_offset, ingest_sqlite_table_from_offset, is_json_file, is_sqlite_file,
    iter_ingestable_files, sqlite_tables,
};
use crate::storage::Storage;

#[derive(Default)]
pub struct TailState {
    json_offsets: BTreeMap<PathBuf, u64>,
    sqlite_offsets: BTreeMap<(PathBuf, String), usize>,
}

pub fn scan_configured_once(
    storage: &Storage,
    config: &Config,
    state: &mut TailState,
) -> Result<usize, String> {
    let mut count = 0;
    for root in &config.transcript_paths {
        count += scan_transcript_path(storage, config, state, Path::new(root))?;
    }
    for root in &config.codex_rollout_paths {
        count += scan_rollout_path(storage, config, state, Path::new(root))?;
    }
    Ok(count)
}

fn scan_transcript_path(
    storage: &Storage,
    config: &Config,
    state: &mut TailState,
    root: &Path,
) -> Result<usize, String> {
    if !root.exists() {
        return Ok(0);
    }
    let mut count = 0;
    for path in iter_ingestable_files(root)? {
        if !is_json_file(&path) {
            continue;
        }
        let size = path.metadata().map_err(|err| err.to_string())?.len();
        let mut offset = *state.json_offsets.get(&path).unwrap_or(&0);
        if size < offset {
            offset = 0;
        }
        if size == offset {
            continue;
        }
        let (added, new_offset) = ingest_json_lines_from_offset(
            storage,
            &path,
            None,
            None,
            offset,
            &config.signal_thresholds,
        )?;
        state.json_offsets.insert(path, new_offset);
        count += added;
    }
    Ok(count)
}

fn scan_rollout_path(
    storage: &Storage,
    config: &Config,
    state: &mut TailState,
    root: &Path,
) -> Result<usize, String> {
    if !root.exists() {
        return Ok(0);
    }
    let mut count = 0;
    for path in iter_ingestable_files(root)? {
        if !is_sqlite_file(&path) {
            continue;
        }
        for table in sqlite_tables(&path)? {
            let key = (path.clone(), table.name.clone());
            let mut offset = *state.sqlite_offsets.get(&key).unwrap_or(&0);
            if table.row_count < offset {
                offset = 0;
            }
            if table.row_count == offset {
                continue;
            }
            let (added, new_offset) = ingest_sqlite_table_from_offset(
                storage,
                &path,
                &table,
                offset,
                Some("codex"),
                None,
                &config.signal_thresholds,
            )?;
            state.sqlite_offsets.insert(key, new_offset);
            count += added;
        }
    }
    Ok(count)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::SignalThresholds;
    use crate::sqlite::{Bind, Database};
    use std::io::Write;

    #[test]
    fn scan_once_imports_only_new_rows_and_lines() {
        let root =
            std::env::temp_dir().join(format!("tranquil-rust-tail-{}", crate::util::now_millis()));
        let transcript_dir = root.join("claude");
        let rollout = root.join("codex.db");
        std::fs::create_dir_all(&transcript_dir).unwrap();
        let transcript = transcript_dir.join("session.jsonl");
        std::fs::write(
            &transcript,
            format!(
                "{}\n",
                serde_json::json!({"type": "user", "session_id": "tail-json", "prompt": "build"})
            ),
        )
        .unwrap();
        let db = Database::open(&rollout).unwrap();
        db.execute("CREATE TABLE rollouts (payload TEXT)", &[])
            .unwrap();
        db.execute(
            "INSERT INTO rollouts (payload) VALUES (?)",
            &[Bind::Text(
                serde_json::to_string(&serde_json::json!({
                    "session_id": "tail-sqlite",
                    "prompt": "inspect",
                }))
                .unwrap(),
            )],
        )
        .unwrap();
        drop(db);
        let config = Config {
            home: root.join("home"),
            db_path: root.join("tranquil.db"),
            token: "tok".to_string(),
            raw_payloads: true,
            signal_thresholds: SignalThresholds::default(),
            kill_switch_enabled: false,
            run_cost_budget_usd: 10.0,
            policy_enabled: false,
            policy_forbidden_paths: Vec::new(),
            policy_forbidden_commands: Vec::new(),
            codex_rollout_paths: vec![rollout.display().to_string()],
            transcript_paths: vec![transcript_dir.display().to_string()],
            tail_interval_seconds: 2.0,
            notification_webhook_url: None,
            notification_command: None,
            replay_command: None,
            judge_command: None,
            trace_sampling_enabled: false,
            trace_sample_rate: 0.05,
            trace_sample_suite: "sampled".to_string(),
            sync_endpoint: None,
            sync_headers: std::collections::BTreeMap::new(),
        };
        let storage = Storage::open(&config.db_path).unwrap();
        let mut state = TailState::default();
        assert_eq!(
            scan_configured_once(&storage, &config, &mut state).unwrap(),
            2
        );
        assert_eq!(
            scan_configured_once(&storage, &config, &mut state).unwrap(),
            0
        );
        std::fs::OpenOptions::new()
            .append(true)
            .open(&transcript)
            .unwrap()
            .write_all(
                format!(
                    "{}\n",
                    serde_json::json!({"session_id": "tail-json", "tool_name": "Bash", "tool_input": {"command": "pytest"}})
                )
                .as_bytes(),
            )
            .unwrap();
        assert_eq!(
            scan_configured_once(&storage, &config, &mut state).unwrap(),
            1
        );
        let _ = std::fs::remove_dir_all(root);
    }
}
