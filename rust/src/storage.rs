use std::path::Path;

use std::collections::{BTreeMap, BTreeSet, HashMap};

use serde::Serialize;
use serde_json::{Value, json};

use crate::config::SignalThresholds;
use crate::sqlite::{Bind, Database};
use crate::util::{git_repo_state, new_id, now_millis, now_ts, stable_id};

const SCHEMA: &str = r#"
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  parent_session_id TEXT,
  depth INTEGER NOT NULL DEFAULT 0,
  agent TEXT NOT NULL,
  agent_version TEXT,
  event_type TEXT NOT NULL,
  source TEXT NOT NULL,
  ts TEXT NOT NULL,
  model TEXT,
  resolved_model TEXT,
  tool_name TEXT,
  duration_ms INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  cost_usd_est REAL,
  permission_decision TEXT,
  repo TEXT,
  branch TEXT,
  cwd TEXT,
  message TEXT,
  raw_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_repo_branch_ts ON events(repo, branch, ts);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_event_type_ts ON events(event_type, ts);
CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  repo TEXT,
  branch TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  last_event_at TEXT NOT NULL,
  status TEXT NOT NULL,
  total_cost_usd_est REAL NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  files_touched INTEGER NOT NULL DEFAULT 0,
  produced_pr INTEGER NOT NULL DEFAULT 0,
  checks_ran INTEGER NOT NULL DEFAULT 0,
  signals_count INTEGER NOT NULL DEFAULT 0,
  first_prompt TEXT,
  latest_message TEXT,
  activity_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_runs_repo_branch_last_event ON runs(repo, branch, last_event_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY,
  signal_key TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  type TEXT NOT NULL,
  severity TEXT NOT NULL,
  fired_at TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  action TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS fixtures (
  fixture_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  suite TEXT NOT NULL DEFAULT 'default',
  prompt TEXT,
  repo_ref_json TEXT NOT NULL,
  recorded_trajectory_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS eval_runs (
  eval_run_id TEXT PRIMARY KEY,
  suite TEXT NOT NULL,
  baseline_eval_run_id TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
  score_id TEXT PRIMARY KEY,
  eval_run_id TEXT NOT NULL,
  fixture_id TEXT NOT NULL,
  scorer TEXT NOT NULL,
  value REAL,
  passed INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  FOREIGN KEY(eval_run_id) REFERENCES eval_runs(eval_run_id) ON DELETE CASCADE,
  FOREIGN KEY(fixture_id) REFERENCES fixtures(fixture_id) ON DELETE CASCADE
);
"#;

pub struct Storage {
    db: Database,
    raw_payloads: bool,
}

#[derive(Debug, Serialize)]
pub struct Stats {
    pub runs: i64,
    pub cost_usd_est: f64,
    pub live: i64,
    pub signaled: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct RunSummary {
    pub run_id: String,
    pub agent: String,
    pub repo: Option<String>,
    pub branch: Option<String>,
    pub started_at: String,
    pub ended_at: Option<String>,
    pub last_event_at: String,
    pub status: String,
    pub total_cost_usd_est: f64,
    pub tool_calls: i64,
    pub files_touched: i64,
    pub produced_pr: bool,
    pub checks_ran: bool,
    pub signals_count: i64,
    pub first_prompt: Option<String>,
    pub latest_message: Option<String>,
    pub labels: BTreeMap<String, Vec<String>>,
    pub subagents_count: i64,
    pub max_depth: i64,
}

#[derive(Debug, Clone, Default)]
pub struct RunFilters {
    pub limit: i64,
    pub status: Option<String>,
    pub agent: Option<String>,
    pub repo: Option<String>,
    pub branch: Option<String>,
    pub labels: Vec<String>,
    pub since: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct SubagentRollup {
    pub subagents_count: i64,
    pub max_depth: i64,
}

#[derive(Debug, Serialize)]
pub struct SubagentRecord {
    pub session_id: String,
    pub parent_session_id: Option<String>,
    pub depth: i64,
    pub started_at: String,
    pub last_event_at: String,
    pub cost_usd_est: f64,
    pub event_count: i64,
    pub tool_calls: i64,
    pub model: Option<String>,
    pub latest_message: Option<String>,
    pub status: String,
}

#[derive(Debug, Serialize)]
pub struct FileTouchSummary {
    pub path: String,
    pub reads: i64,
    pub writes: i64,
    pub events: i64,
    pub tools: Vec<String>,
    pub last_event_at: Option<String>,
    pub reread_thrash: bool,
}

#[derive(Debug, Serialize)]
pub struct RunDiff {
    pub a: RunSummary,
    pub b: RunSummary,
    pub delta: Value,
    pub tools: Value,
}

#[derive(Debug, Serialize)]
pub struct SignalRecord {
    pub signal_id: String,
    pub signal_key: String,
    pub run_id: String,
    #[serde(rename = "type")]
    pub signal_type: String,
    pub severity: String,
    pub fired_at: String,
    pub evidence: Value,
    pub action: Option<String>,
    pub active: bool,
}

#[derive(Debug, Serialize)]
pub struct CostRollup {
    pub key: String,
    pub runs: i64,
    pub cost_usd_est: f64,
}

#[derive(Debug, Serialize)]
pub struct PurgeCounts {
    pub events: i64,
    pub runs: i64,
    pub signals: i64,
    pub fixtures: i64,
    pub eval_runs: i64,
    pub scores: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct FixtureRecord {
    pub fixture_id: String,
    pub run_id: String,
    pub suite: String,
    pub prompt: Option<String>,
    pub repo_ref: Value,
    pub recorded_trajectory: Vec<Value>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct EvalRunRecord {
    pub eval_run_id: String,
    pub suite: String,
    pub baseline_eval_run_id: Option<String>,
    pub started_at: String,
    pub ended_at: Option<String>,
    pub status: String,
    pub score_count: i64,
    pub passed_count: i64,
    pub failed_count: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct ScoreRecord {
    pub score_id: String,
    pub eval_run_id: String,
    pub fixture_id: String,
    pub scorer: String,
    pub value: Option<f64>,
    pub passed: bool,
    pub detail: Value,
}

#[derive(Debug, Clone, Serialize)]
pub struct RunScoreRecord {
    pub score_id: String,
    pub eval_run_id: String,
    pub fixture_id: String,
    pub scorer: String,
    pub value: Option<f64>,
    pub passed: bool,
    pub detail: Value,
    pub suite: String,
    pub eval_status: String,
    pub eval_started_at: String,
    pub eval_ended_at: Option<String>,
}

impl Storage {
    #[allow(dead_code)]
    pub fn open(path: &Path) -> Result<Self, String> {
        Self::open_with_raw_payloads(path, true)
    }

    pub fn open_with_raw_payloads(path: &Path, raw_payloads: bool) -> Result<Self, String> {
        let db = Database::open(path)?;
        db.exec(SCHEMA)?;
        Ok(Self { db, raw_payloads })
    }

    #[allow(dead_code)]
    pub fn record_event(&self, event: &Value) -> Result<(), String> {
        self.record_event_with_thresholds(event, &SignalThresholds::default())
    }

    pub fn record_event_with_thresholds(
        &self,
        event: &Value,
        thresholds: &SignalThresholds,
    ) -> Result<(), String> {
        let tool = event
            .get("tool")
            .filter(|value| value.is_object())
            .unwrap_or(&Value::Null);
        let usage = event
            .get("usage")
            .filter(|value| value.is_object())
            .unwrap_or(&Value::Null);
        let permission = event
            .get("permission")
            .filter(|value| value.is_object())
            .unwrap_or(&Value::Null);
        let context = event
            .get("context")
            .filter(|value| value.is_object())
            .unwrap_or(&Value::Null);
        self.db.execute(
            r#"
            INSERT OR IGNORE INTO events (
              event_id, run_id, session_id, parent_session_id, depth, agent, agent_version,
              event_type, source, ts, model, resolved_model, tool_name, duration_ms,
              input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
              cost_usd_est, permission_decision, repo, branch, cwd, message, raw_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            "#,
            &[
                bind_text(event, "event_id"),
                bind_text(event, "run_id"),
                bind_text(event, "session_id"),
                bind_opt_text(event.get("parent_session_id")),
                Bind::Int(event.get("depth").and_then(Value::as_i64).unwrap_or(0)),
                bind_text(event, "agent"),
                bind_opt_text(event.get("agent_version")),
                bind_text(event, "event_type"),
                bind_text(event, "source"),
                bind_text(event, "ts"),
                bind_opt_text(event.get("model")),
                bind_opt_text(event.get("resolved_model")),
                bind_opt_text(tool.get("name")),
                bind_opt_i64(tool.get("duration_ms")),
                bind_opt_i64(usage.get("input_tokens")),
                bind_opt_i64(usage.get("output_tokens")),
                bind_opt_i64(usage.get("cache_read_tokens")),
                bind_opt_i64(usage.get("cache_write_tokens")),
                bind_opt_f64(usage.get("cost_usd_est")),
                bind_opt_text(permission.get("decision")),
                bind_opt_text(context.get("repo")),
                bind_opt_text(context.get("branch")),
                bind_opt_text(context.get("cwd")),
                bind_opt_text(event.get("message")),
                Bind::Text(
                    serde_json::to_string(&self.persistable_event(event))
                        .map_err(|err| err.to_string())?,
                ),
                Bind::Text(now_ts()),
            ],
        )?;
        self.upsert_run(event)?;
        self.refresh_run_rollups(str_field(event, "run_id")?)?;
        self.evaluate_run_signals(str_field(event, "run_id")?, thresholds)?;
        Ok(())
    }

    pub fn stats(&self) -> Result<Stats, String> {
        let rows = self.db.query(
            r#"
            SELECT COUNT(*) AS runs,
                   COALESCE(SUM(total_cost_usd_est), 0) AS cost,
                   SUM(CASE WHEN status IN ('running','waiting') THEN 1 ELSE 0 END) AS live,
                   SUM(CASE WHEN signals_count > 0 THEN 1 ELSE 0 END) AS signaled
              FROM runs
            "#,
            &[],
        )?;
        let row = rows.first();
        Ok(Stats {
            runs: row
                .and_then(|r| r.get("runs"))
                .and_then(|v| v.parse().ok())
                .unwrap_or(0),
            cost_usd_est: row
                .and_then(|r| r.get("cost"))
                .and_then(|v| v.parse().ok())
                .unwrap_or(0.0),
            live: row
                .and_then(|r| r.get("live"))
                .and_then(|v| v.parse().ok())
                .unwrap_or(0),
            signaled: row
                .and_then(|r| r.get("signaled"))
                .and_then(|v| v.parse().ok())
                .unwrap_or(0),
        })
    }

    pub fn list_runs(&self, limit: i64) -> Result<Vec<RunSummary>, String> {
        self.list_runs_filtered(&RunFilters {
            limit,
            ..RunFilters::default()
        })
    }

    pub fn list_runs_filtered(&self, filters: &RunFilters) -> Result<Vec<RunSummary>, String> {
        let mut clauses = Vec::new();
        let mut params = Vec::new();
        for (column, value) in [
            ("status", filters.status.as_deref()),
            ("agent", filters.agent.as_deref()),
            ("repo", filters.repo.as_deref()),
            ("branch", filters.branch.as_deref()),
        ] {
            if let Some(value) = value.filter(|value| !value.is_empty()) {
                clauses.push(format!("{column} = ?"));
                params.push(Bind::Text(value.to_string()));
            }
        }
        if let Some(since) = filters.since.as_deref().filter(|value| !value.is_empty()) {
            clauses.push("last_event_at >= ?".to_string());
            params.push(Bind::Text(since.to_string()));
        }
        let where_sql = if clauses.is_empty() {
            String::new()
        } else {
            format!(" WHERE {}", clauses.join(" AND "))
        };
        let label_filters = normalize_label_filters(&filters.labels);
        let mut sql = format!("SELECT * FROM runs{where_sql} ORDER BY last_event_at DESC");
        if label_filters.is_empty() {
            sql.push_str(" LIMIT ?");
            params.push(Bind::Int(filters.limit.max(1)));
        }
        let rows = self.db.query(&sql, &params)?;
        let mut runs = Vec::new();
        for row in rows {
            let run = self.enrich_run(decode_run(row)?)?;
            if label_filters.is_empty() || labels_match(&run.labels, &label_filters) {
                runs.push(run);
                if runs.len() >= filters.limit.max(1) as usize {
                    break;
                }
            }
        }
        Ok(runs)
    }

    pub fn get_run(&self, run_id: &str) -> Result<Option<RunSummary>, String> {
        let rows = self.db.query(
            "SELECT * FROM runs WHERE run_id = ?",
            &[Bind::Text(run_id.to_string())],
        )?;
        rows.into_iter()
            .next()
            .map(decode_run)
            .transpose()?
            .map(|run| self.enrich_run(run))
            .transpose()
    }

    pub fn get_run_events(&self, run_id: &str) -> Result<Vec<Value>, String> {
        Ok(self
            .db
            .query(
                "SELECT raw_json FROM events WHERE run_id = ? ORDER BY ts, created_at",
                &[Bind::Text(run_id.to_string())],
            )?
            .into_iter()
            .filter_map(|row| {
                row.get("raw_json")
                    .and_then(|text| serde_json::from_str(text).ok())
            })
            .collect())
    }

    pub fn get_run_display_events(&self, run_id: &str) -> Result<Vec<Value>, String> {
        let mut events = self.get_run_events(run_id)?;
        for event in &mut events {
            if let Some(diff) = event_diff_preview(event, 12_000) {
                if let Some(object) = event.as_object_mut() {
                    object.insert("diff".to_string(), diff);
                }
            }
        }
        Ok(events)
    }

    pub fn run_labels(&self, run_id: &str) -> Result<BTreeMap<String, Vec<String>>, String> {
        let rows = self.db.query(
            "SELECT raw_json FROM events WHERE run_id = ?",
            &[Bind::Text(run_id.to_string())],
        )?;
        let mut labels: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        for row in rows {
            let Some(raw_json) = row.get("raw_json") else {
                continue;
            };
            let Ok(event) = serde_json::from_str::<Value>(raw_json) else {
                continue;
            };
            let Some(raw_labels) = event.get("labels").and_then(Value::as_object) else {
                continue;
            };
            for (key, value) in raw_labels {
                labels
                    .entry(key.to_string())
                    .or_default()
                    .insert(label_value_to_string(value));
            }
        }
        Ok(labels
            .into_iter()
            .map(|(key, values)| (key, values.into_iter().collect()))
            .collect())
    }

    pub fn subagent_rollup(&self, run_id: &str) -> Result<SubagentRollup, String> {
        let rows = self.db.query(
            r#"
            SELECT COUNT(*) AS subagents_count,
                   COALESCE(MAX(depth), 0) AS max_depth
              FROM (
                SELECT session_id,
                       MAX(depth) AS depth,
                       MAX(CASE WHEN parent_session_id IS NOT NULL OR depth > 0 THEN 1 ELSE 0 END) AS is_subagent
                  FROM events
                 WHERE run_id = ?
                 GROUP BY session_id
              )
             WHERE is_subagent = 1
            "#,
            &[Bind::Text(run_id.to_string())],
        )?;
        Ok(SubagentRollup {
            subagents_count: scalar_i64(&rows, "subagents_count"),
            max_depth: scalar_i64(&rows, "max_depth"),
        })
    }

    pub fn list_subagents(&self, run_id: &str) -> Result<Vec<SubagentRecord>, String> {
        let rows = self.db.query(
            r#"
            SELECT session_id,
                   MAX(parent_session_id) AS parent_session_id,
                   MAX(depth) AS depth,
                   MIN(ts) AS started_at,
                   MAX(ts) AS last_event_at,
                   COALESCE(SUM(cost_usd_est), 0) AS cost_usd_est,
                   COUNT(*) AS event_count,
                   SUM(CASE WHEN event_type IN ('post_tool','tool_failure','pre_tool') THEN 1 ELSE 0 END) AS tool_calls,
                   SUM(CASE WHEN event_type = 'tool_failure' THEN 1 ELSE 0 END) AS failures,
                   SUM(CASE WHEN event_type IN ('session_end','stop','task_completed','subagent_stop') THEN 1 ELSE 0 END) AS completions,
                   COALESCE(MAX(resolved_model), MAX(model)) AS model,
                   MAX(message) AS latest_message
              FROM events
             WHERE run_id = ?
             GROUP BY session_id
            HAVING MAX(CASE WHEN parent_session_id IS NOT NULL OR depth > 0 THEN 1 ELSE 0 END) = 1
             ORDER BY MAX(depth), MIN(ts), session_id
            "#,
            &[Bind::Text(run_id.to_string())],
        )?;
        rows.into_iter()
            .map(|row| {
                let failures = optional(&row, "failures")
                    .and_then(|text| text.parse::<i64>().ok())
                    .unwrap_or(0);
                let completions = optional(&row, "completions")
                    .and_then(|text| text.parse::<i64>().ok())
                    .unwrap_or(0);
                let status = if failures > 0 {
                    "failed"
                } else if completions > 0 {
                    "completed"
                } else {
                    "running"
                }
                .to_string();
                Ok(SubagentRecord {
                    session_id: required(&row, "session_id")?,
                    parent_session_id: optional(&row, "parent_session_id"),
                    depth: optional(&row, "depth")
                        .and_then(|text| text.parse().ok())
                        .unwrap_or(0),
                    started_at: required(&row, "started_at")?,
                    last_event_at: required(&row, "last_event_at")?,
                    cost_usd_est: optional(&row, "cost_usd_est")
                        .and_then(|text| text.parse().ok())
                        .unwrap_or(0.0),
                    event_count: optional(&row, "event_count")
                        .and_then(|text| text.parse().ok())
                        .unwrap_or(0),
                    tool_calls: optional(&row, "tool_calls")
                        .and_then(|text| text.parse().ok())
                        .unwrap_or(0),
                    model: optional(&row, "model"),
                    latest_message: optional(&row, "latest_message"),
                    status,
                })
            })
            .collect()
    }

    pub fn file_touch_summary(&self, run_id: &str) -> Result<Vec<FileTouchSummary>, String> {
        let rows = self.db.query(
            r#"
            SELECT ts, event_type, tool_name, raw_json
              FROM events
             WHERE run_id = ?
               AND event_type IN ('post_tool','tool_failure','file_changed')
             ORDER BY ts, created_at
            "#,
            &[Bind::Text(run_id.to_string())],
        )?;
        let mut by_path: BTreeMap<String, FileTouchSummary> = BTreeMap::new();
        for row in rows {
            let event = row
                .get("raw_json")
                .and_then(|text| serde_json::from_str::<Value>(text).ok())
                .unwrap_or(Value::Null);
            let paths = extract_paths(&event);
            if paths.is_empty() {
                continue;
            }
            let access = file_access_kind(&event);
            for path in paths {
                let entry = by_path.entry(path.clone()).or_insert(FileTouchSummary {
                    path,
                    reads: 0,
                    writes: 0,
                    events: 0,
                    tools: Vec::new(),
                    last_event_at: None,
                    reread_thrash: false,
                });
                entry.events += 1;
                if access == "write" {
                    entry.writes += 1;
                } else {
                    entry.reads += 1;
                }
                let tool = optional(&row, "tool_name")
                    .or_else(|| optional(&row, "event_type"))
                    .unwrap_or_default();
                if !tool.is_empty() && !entry.tools.iter().any(|existing| existing == &tool) {
                    entry.tools.push(tool);
                }
                entry.last_event_at = optional(&row, "ts");
            }
        }
        let mut values = by_path.into_values().collect::<Vec<_>>();
        let reread_threshold = SignalThresholds::default().reread_repeats;
        for value in &mut values {
            value.reread_thrash = value.reads >= reread_threshold;
        }
        values.sort_by(|left, right| {
            left.reread_thrash
                .cmp(&right.reread_thrash)
                .reverse()
                .then_with(|| left.path.cmp(&right.path))
        });
        Ok(values)
    }

    pub fn diff_runs(&self, a: &str, b: &str) -> Result<RunDiff, String> {
        let run_a = self
            .get_run(a)?
            .ok_or_else(|| format!("run not found: {a}"))?;
        let run_b = self
            .get_run(b)?
            .ok_or_else(|| format!("run not found: {b}"))?;
        let events_a = self.get_run_events(a)?;
        let events_b = self.get_run_events(b)?;
        Ok(RunDiff {
            delta: json!({
                "cost_usd_est": run_b.total_cost_usd_est - run_a.total_cost_usd_est,
                "tool_calls": run_b.tool_calls - run_a.tool_calls,
                "files_touched": run_b.files_touched - run_a.files_touched,
                "signals_count": run_b.signals_count - run_a.signals_count,
            }),
            tools: json!({
                "a": events_a.iter().map(tool_or_event_type).collect::<Vec<_>>(),
                "b": events_b.iter().map(tool_or_event_type).collect::<Vec<_>>(),
            }),
            a: run_a,
            b: run_b,
        })
    }

    pub fn event_exists(&self, event_id: &str) -> Result<bool, String> {
        Ok(!self
            .db
            .query(
                "SELECT 1 AS value FROM events WHERE event_id = ? LIMIT 1",
                &[Bind::Text(event_id.to_string())],
            )?
            .is_empty())
    }

    pub fn list_signals(
        &self,
        active: Option<bool>,
        limit: i64,
    ) -> Result<Vec<SignalRecord>, String> {
        let rows = if let Some(active) = active {
            self.db.query(
                "SELECT * FROM signals WHERE active = ? ORDER BY fired_at DESC LIMIT ?",
                &[Bind::Int(if active { 1 } else { 0 }), Bind::Int(limit)],
            )?
        } else {
            self.db.query(
                "SELECT * FROM signals ORDER BY fired_at DESC LIMIT ?",
                &[Bind::Int(limit)],
            )?
        };
        rows.into_iter().map(decode_signal).collect()
    }

    pub fn list_run_signals(&self, run_id: &str) -> Result<Vec<SignalRecord>, String> {
        let rows = self.db.query(
            "SELECT * FROM signals WHERE run_id = ? ORDER BY fired_at DESC",
            &[Bind::Text(run_id.to_string())],
        )?;
        rows.into_iter().map(decode_signal).collect()
    }

    pub fn has_active_signal(&self, run_id: &str, signal_type: &str) -> Result<bool, String> {
        let rows = self.db.query(
            "SELECT 1 AS value FROM signals WHERE run_id = ? AND type = ? AND active = 1 LIMIT 1",
            &[
                Bind::Text(run_id.to_string()),
                Bind::Text(signal_type.to_string()),
            ],
        )?;
        Ok(!rows.is_empty())
    }

    pub fn request_stop(&self, run_id: &str, reason: &str) -> Result<bool, String> {
        if self.get_run(run_id)?.is_none() {
            return Err(format!("run not found: {run_id}"));
        }
        self.add_signal(
            run_id,
            "stop_requested",
            "high",
            json!({
                "reason": reason,
                "fingerprint": "manual_stop",
                "message": "manual stop requested; future pre-tool hooks will be denied",
            }),
            Some("deny_pre_tool"),
        )
    }

    pub fn add_signal(
        &self,
        run_id: &str,
        signal_type: &str,
        severity: &str,
        evidence: Value,
        action: Option<&str>,
    ) -> Result<bool, String> {
        let fingerprint = evidence
            .get("fingerprint")
            .or_else(|| evidence.get("path"))
            .or_else(|| evidence.get("tool"))
            .or_else(|| evidence.get("reason"))
            .and_then(Value::as_str)
            .unwrap_or("default");
        let key = format!("{run_id}:{signal_type}:{fingerprint}");
        let existed = !self
            .db
            .query(
                "SELECT 1 AS value FROM signals WHERE signal_key = ? LIMIT 1",
                &[Bind::Text(key.clone())],
            )?
            .is_empty();
        self.db.execute(
            r#"
            INSERT OR IGNORE INTO signals (
              signal_id, signal_key, run_id, type, severity, fired_at, evidence_json, action, active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            "#,
            &[
                Bind::Text(new_id("sig")),
                Bind::Text(key.clone()),
                Bind::Text(run_id.to_string()),
                Bind::Text(signal_type.to_string()),
                Bind::Text(severity.to_string()),
                Bind::Text(now_ts()),
                Bind::Text(serde_json::to_string(&evidence).map_err(|err| err.to_string())?),
                bind_optional_str(action),
            ],
        )?;
        self.refresh_run_rollups(run_id)?;
        Ok(!existed)
    }

    pub fn cost_rollup(&self, group_by: &str) -> Result<Vec<CostRollup>, String> {
        let column = match group_by {
            "agent" | "repo" | "branch" | "status" => group_by,
            _ => return Err("group_by must be one of agent, repo, branch, status".to_string()),
        };
        let rows = self.db.query(
            &format!(
                "SELECT COALESCE({column}, 'unknown') AS key, COUNT(*) AS runs, COALESCE(SUM(total_cost_usd_est), 0) AS cost_usd_est FROM runs GROUP BY COALESCE({column}, 'unknown') ORDER BY cost_usd_est DESC, runs DESC"
            ),
            &[],
        )?;
        Ok(rows
            .into_iter()
            .map(|row| CostRollup {
                key: optional(&row, "key").unwrap_or_else(|| "unknown".to_string()),
                runs: optional(&row, "runs")
                    .and_then(|text| text.parse().ok())
                    .unwrap_or(0),
                cost_usd_est: optional(&row, "cost_usd_est")
                    .and_then(|text| text.parse().ok())
                    .unwrap_or(0.0),
            })
            .collect())
    }

    pub fn purge(&self, all_data: bool) -> Result<PurgeCounts, String> {
        if !all_data {
            return Err("Rust purge currently requires --all".to_string());
        }
        let scores = self.delete_count("scores", None)?;
        let eval_runs = self.delete_count("eval_runs", None)?;
        let fixtures = self.delete_count("fixtures", None)?;
        let signals = self.delete_count("signals", None)?;
        let events = self.delete_count("events", None)?;
        let runs = self.delete_count("runs", None)?;
        Ok(PurgeCounts {
            events,
            runs,
            signals,
            fixtures,
            eval_runs,
            scores,
        })
    }

    pub fn purge_older_than(&self, older_than_days: i64) -> Result<PurgeCounts, String> {
        if older_than_days < 0 {
            return Err("--older-than must be non-negative".to_string());
        }
        let cutoff_ms = (now_millis() as i128) - (older_than_days as i128 * 86_400_000);
        let cutoff_ms = cutoff_ms.max(i64::MIN as i128).min(i64::MAX as i128) as i64;
        let cutoff_s = cutoff_ms as f64 / 1000.0;
        let rows = self.db.query(
            r#"
            SELECT run_id
              FROM runs
             WHERE last_event_at <> ''
               AND last_event_at NOT GLOB '*[^0-9.]*'
               AND (
                    (CAST(last_event_at AS REAL) >= 100000000000.0 AND CAST(last_event_at AS REAL) < ?)
                 OR (CAST(last_event_at AS REAL) < 100000000000.0 AND CAST(last_event_at AS REAL) < ?)
               )
            "#,
            &[Bind::Int(cutoff_ms), Bind::Float(cutoff_s)],
        )?;
        let mut counts = PurgeCounts {
            events: 0,
            runs: 0,
            signals: 0,
            fixtures: 0,
            eval_runs: 0,
            scores: 0,
        };
        for row in rows {
            let Some(run_id) = row.get("run_id") else {
                continue;
            };
            let has_fixture = !self
                .db
                .query(
                    "SELECT 1 AS value FROM fixtures WHERE run_id = ? LIMIT 1",
                    &[Bind::Text(run_id.to_string())],
                )?
                .is_empty();
            counts.signals += self.delete_run_rows("signals", run_id)?;
            counts.events += self.delete_run_rows("events", run_id)?;
            if has_fixture {
                self.db.execute(
                    "UPDATE runs SET signals_count = 0 WHERE run_id = ?",
                    &[Bind::Text(run_id.to_string())],
                )?;
            } else {
                counts.runs += self.delete_run_rows("runs", run_id)?;
            }
        }
        Ok(counts)
    }

    pub fn create_fixture(
        &self,
        run_id: &str,
        suite: &str,
        fixture_id: Option<&str>,
    ) -> Result<FixtureRecord, String> {
        self.create_fixture_with_options(run_id, suite, fixture_id, None, None, None)
    }

    pub fn create_fixture_with_options(
        &self,
        run_id: &str,
        suite: &str,
        fixture_id: Option<&str>,
        cost_budget_usd: Option<f64>,
        forbidden_paths: Option<Vec<String>>,
        repo_ref_extra: Option<Value>,
    ) -> Result<FixtureRecord, String> {
        let run = self
            .get_run(run_id)?
            .ok_or_else(|| format!("run not found: {run_id}"))?;
        let events = self.get_run_events(run_id)?;
        let prompt = run
            .first_prompt
            .clone()
            .or_else(|| first_user_prompt(&events));
        let mut repo_ref = git_repo_state(first_event_cwd(&events).as_deref());
        repo_ref["repo"] = serde_json::to_value(run.repo).unwrap_or(Value::Null);
        if repo_ref.get("branch").is_none() || repo_ref.get("branch").is_some_and(Value::is_null) {
            repo_ref["branch"] = serde_json::to_value(run.branch).unwrap_or(Value::Null);
        }
        if let Some(cost_budget_usd) = cost_budget_usd {
            repo_ref["budgets"] = json!({"cost_usd": cost_budget_usd});
        }
        if let Some(forbidden_paths) = forbidden_paths {
            repo_ref["forbidden_paths"] = json!(forbidden_paths);
        }
        merge_object(&mut repo_ref, repo_ref_extra);
        let fixture_id = fixture_id
            .map(ToString::to_string)
            .unwrap_or_else(|| new_id("fix"));
        self.db.execute(
            r#"
            INSERT INTO fixtures (
              fixture_id, run_id, suite, prompt, repo_ref_json, recorded_trajectory_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            "#,
            &[
                Bind::Text(fixture_id.clone()),
                Bind::Text(run_id.to_string()),
                Bind::Text(suite.to_string()),
                bind_optional_str(prompt.as_deref()),
                Bind::Text(serde_json::to_string(&repo_ref).map_err(|err| err.to_string())?),
                Bind::Text(serde_json::to_string(&events).map_err(|err| err.to_string())?),
                Bind::Text(now_ts()),
            ],
        )?;
        self.get_fixture(&fixture_id)?
            .ok_or_else(|| format!("fixture not found after insert: {fixture_id}"))
    }

    pub fn upsert_fixture_definition(
        &self,
        fixture_id: &str,
        run_id: &str,
        suite: &str,
        prompt: Option<&str>,
        repo_ref: Option<Value>,
        budgets: Option<Value>,
        forbidden_paths: Option<Vec<String>>,
        rubric: Option<&str>,
        reference: Option<&str>,
    ) -> Result<FixtureRecord, String> {
        let run = self
            .get_run(run_id)?
            .ok_or_else(|| format!("run not found: {run_id}"))?;
        let events = self.get_run_events(run_id)?;
        let prompt = prompt
            .map(ToString::to_string)
            .or_else(|| run.first_prompt.clone())
            .or_else(|| first_user_prompt(&events));
        let mut merged_repo_ref =
            repo_ref.unwrap_or_else(|| git_repo_state(first_event_cwd(&events).as_deref()));
        if !merged_repo_ref.is_object() {
            merged_repo_ref = json!({});
        }
        if merged_repo_ref.get("repo").is_none() {
            merged_repo_ref["repo"] = serde_json::to_value(run.repo).unwrap_or(Value::Null);
        }
        if merged_repo_ref.get("branch").is_none() {
            merged_repo_ref["branch"] = serde_json::to_value(run.branch).unwrap_or(Value::Null);
        }
        if let Some(budgets) = budgets {
            merged_repo_ref["budgets"] = budgets;
        }
        if let Some(forbidden_paths) = forbidden_paths {
            merged_repo_ref["forbidden_paths"] = json!(forbidden_paths);
        }
        if let Some(rubric) = rubric {
            merged_repo_ref["rubric"] = json!(rubric);
        }
        if let Some(reference) = reference {
            merged_repo_ref["reference"] = json!(reference);
        }
        self.db.execute(
            r#"
            INSERT INTO fixtures (
              fixture_id, run_id, suite, prompt, repo_ref_json, recorded_trajectory_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fixture_id) DO UPDATE SET
              run_id = excluded.run_id,
              suite = excluded.suite,
              prompt = excluded.prompt,
              repo_ref_json = excluded.repo_ref_json,
              recorded_trajectory_json = excluded.recorded_trajectory_json
            "#,
            &[
                Bind::Text(fixture_id.to_string()),
                Bind::Text(run_id.to_string()),
                Bind::Text(suite.to_string()),
                bind_optional_str(prompt.as_deref()),
                Bind::Text(serde_json::to_string(&merged_repo_ref).map_err(|err| err.to_string())?),
                Bind::Text(serde_json::to_string(&events).map_err(|err| err.to_string())?),
                Bind::Text(now_ts()),
            ],
        )?;
        self.get_fixture(fixture_id)?
            .ok_or_else(|| format!("fixture not found after upsert: {fixture_id}"))
    }

    pub fn create_fixtures_from_signals(&self, suite: &str) -> Result<Vec<FixtureRecord>, String> {
        let rows = self.db.query(
            r#"
            SELECT DISTINCT s.run_id
              FROM signals s
             WHERE s.active = 1
               AND NOT EXISTS (
                 SELECT 1 FROM fixtures f WHERE f.run_id = s.run_id AND f.suite = ?
               )
             ORDER BY s.fired_at DESC
            "#,
            &[Bind::Text(suite.to_string())],
        )?;
        let mut fixtures = Vec::new();
        for row in rows {
            if let Some(run_id) = row.get("run_id") {
                fixtures.push(self.create_fixture(run_id, suite, None)?);
            }
        }
        Ok(fixtures)
    }

    #[allow(dead_code)]
    pub fn sample_runs(
        &self,
        suite: &str,
        sample_rate: f64,
        limit: i64,
        status: &str,
    ) -> Result<Vec<FixtureRecord>, String> {
        self.sample_runs_filtered(
            suite,
            sample_rate,
            limit,
            &RunFilters {
                limit: limit.max(1) * 20,
                status: Some(status.to_string()),
                ..RunFilters::default()
            },
            status,
        )
    }

    pub fn sample_runs_filtered(
        &self,
        suite: &str,
        sample_rate: f64,
        limit: i64,
        filters: &RunFilters,
        required_status: &str,
    ) -> Result<Vec<FixtureRecord>, String> {
        let mut candidate_filters = filters.clone();
        candidate_filters.limit = candidate_filters.limit.max(limit.max(1) * 20);
        if candidate_filters.status.is_none() {
            candidate_filters.status = Some(required_status.to_string());
        }
        let mut fixtures = Vec::new();
        for run in self.list_runs_filtered(&candidate_filters)? {
            if fixtures.len() >= limit.max(0) as usize {
                break;
            }
            if let Some(fixture) =
                self.sample_run_if_eligible(&run.run_id, suite, sample_rate, required_status)?
            {
                fixtures.push(fixture);
            }
        }
        Ok(fixtures)
    }

    pub fn sample_run_if_eligible(
        &self,
        run_id: &str,
        suite: &str,
        sample_rate: f64,
        required_status: &str,
    ) -> Result<Option<FixtureRecord>, String> {
        let sample_rate = sample_rate.clamp(0.0, 1.0);
        if !should_sample_run(run_id, sample_rate) {
            return Ok(None);
        }
        let Some(run) = self.get_run(run_id)? else {
            return Ok(None);
        };
        if run.status != required_status {
            return Ok(None);
        }
        if self.fixture_for_run(run_id, suite)?.is_some() {
            return Ok(None);
        }
        let fixture_id = stable_id(
            "fix",
            &["sample".to_string(), suite.to_string(), run_id.to_string()],
        );
        self.create_fixture_with_options(
            run_id,
            suite,
            Some(&fixture_id),
            None,
            None,
            Some(json!({"sampled_from": "production_trace", "sample_rate": sample_rate})),
        )
        .map(Some)
    }

    pub fn fixture_for_run(
        &self,
        run_id: &str,
        suite: &str,
    ) -> Result<Option<FixtureRecord>, String> {
        let rows = self.db.query(
            "SELECT * FROM fixtures WHERE run_id = ? AND suite = ? ORDER BY created_at DESC LIMIT 1",
            &[Bind::Text(run_id.to_string()), Bind::Text(suite.to_string())],
        )?;
        rows.into_iter().next().map(decode_fixture).transpose()
    }

    pub fn get_fixture(&self, fixture_id: &str) -> Result<Option<FixtureRecord>, String> {
        let rows = self.db.query(
            "SELECT * FROM fixtures WHERE fixture_id = ?",
            &[Bind::Text(fixture_id.to_string())],
        )?;
        rows.into_iter().next().map(decode_fixture).transpose()
    }

    pub fn list_fixtures(&self, suite: Option<&str>) -> Result<Vec<FixtureRecord>, String> {
        let rows = if let Some(suite) = suite {
            self.db.query(
                "SELECT * FROM fixtures WHERE suite = ? ORDER BY created_at DESC",
                &[Bind::Text(suite.to_string())],
            )?
        } else {
            self.db
                .query("SELECT * FROM fixtures ORDER BY created_at DESC", &[])?
        };
        rows.into_iter().map(decode_fixture).collect()
    }

    pub fn create_eval_run(
        &self,
        suite: &str,
        status: &str,
        baseline_eval_run_id: Option<&str>,
    ) -> Result<String, String> {
        let eval_run_id = new_id("eval");
        self.db.execute(
            r#"
            INSERT INTO eval_runs (eval_run_id, suite, baseline_eval_run_id, started_at, status)
            VALUES (?, ?, ?, ?, ?)
            "#,
            &[
                Bind::Text(eval_run_id.clone()),
                Bind::Text(suite.to_string()),
                bind_optional_str(baseline_eval_run_id),
                Bind::Text(now_ts()),
                Bind::Text(status.to_string()),
            ],
        )?;
        Ok(eval_run_id)
    }

    pub fn finish_eval_run(&self, eval_run_id: &str, status: &str) -> Result<(), String> {
        self.db.execute(
            "UPDATE eval_runs SET status = ?, ended_at = ? WHERE eval_run_id = ?",
            &[
                Bind::Text(status.to_string()),
                Bind::Text(now_ts()),
                Bind::Text(eval_run_id.to_string()),
            ],
        )
    }

    pub fn add_score(
        &self,
        eval_run_id: &str,
        fixture_id: &str,
        scorer: &str,
        value: Option<f64>,
        passed: bool,
        detail: Value,
    ) -> Result<(), String> {
        self.db.execute(
            r#"
            INSERT INTO scores (score_id, eval_run_id, fixture_id, scorer, value, passed, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            "#,
            &[
                Bind::Text(new_id("score")),
                Bind::Text(eval_run_id.to_string()),
                Bind::Text(fixture_id.to_string()),
                Bind::Text(scorer.to_string()),
                value.map(Bind::Float).unwrap_or(Bind::Null),
                Bind::Int(if passed { 1 } else { 0 }),
                Bind::Text(serde_json::to_string(&detail).map_err(|err| err.to_string())?),
            ],
        )
    }

    pub fn list_scores(&self, eval_run_id: &str) -> Result<Vec<ScoreRecord>, String> {
        let rows = self.db.query(
            "SELECT * FROM scores WHERE eval_run_id = ? ORDER BY fixture_id, scorer",
            &[Bind::Text(eval_run_id.to_string())],
        )?;
        rows.into_iter().map(decode_score).collect()
    }

    pub fn list_run_scores(&self, run_id: &str, limit: i64) -> Result<Vec<RunScoreRecord>, String> {
        let rows = self.db.query(
            r#"
            SELECT s.*,
                   e.suite,
                   e.status AS eval_status,
                   e.started_at AS eval_started_at,
                   e.ended_at AS eval_ended_at
              FROM scores s
              JOIN fixtures f ON f.fixture_id = s.fixture_id
              JOIN eval_runs e ON e.eval_run_id = s.eval_run_id
             WHERE f.run_id = ?
             ORDER BY COALESCE(e.ended_at, e.started_at) DESC, s.scorer
             LIMIT ?
            "#,
            &[Bind::Text(run_id.to_string()), Bind::Int(limit)],
        )?;
        rows.into_iter().map(decode_run_score).collect()
    }

    pub fn latest_eval_run(
        &self,
        suite: &str,
        status: Option<&str>,
    ) -> Result<Option<EvalRunRecord>, String> {
        let rows = if let Some(status) = status {
            self.db.query(
                r#"
                SELECT e.*,
                       COUNT(s.score_id) AS score_count,
                       COALESCE(SUM(CASE WHEN s.passed = 1 THEN 1 ELSE 0 END), 0) AS passed_count,
                       COALESCE(SUM(CASE WHEN s.passed = 0 THEN 1 ELSE 0 END), 0) AS failed_count
                  FROM eval_runs e
                  LEFT JOIN scores s ON s.eval_run_id = e.eval_run_id
                 WHERE e.suite = ? AND e.status = ?
                 GROUP BY e.eval_run_id
                 ORDER BY COALESCE(e.ended_at, e.started_at) DESC, e.started_at DESC
                 LIMIT 1
                "#,
                &[
                    Bind::Text(suite.to_string()),
                    Bind::Text(status.to_string()),
                ],
            )?
        } else {
            self.db.query(
                r#"
                SELECT e.*,
                       COUNT(s.score_id) AS score_count,
                       COALESCE(SUM(CASE WHEN s.passed = 1 THEN 1 ELSE 0 END), 0) AS passed_count,
                       COALESCE(SUM(CASE WHEN s.passed = 0 THEN 1 ELSE 0 END), 0) AS failed_count
                  FROM eval_runs e
                  LEFT JOIN scores s ON s.eval_run_id = e.eval_run_id
                 WHERE e.suite = ?
                 GROUP BY e.eval_run_id
                 ORDER BY COALESCE(e.ended_at, e.started_at) DESC, e.started_at DESC
                 LIMIT 1
                "#,
                &[Bind::Text(suite.to_string())],
            )?
        };
        rows.into_iter().next().map(decode_eval_run).transpose()
    }

    #[allow(dead_code)]
    pub fn get_eval_run(&self, eval_run_id: &str) -> Result<Option<EvalRunRecord>, String> {
        let rows = self.db.query(
            r#"
            SELECT e.*,
                   COUNT(s.score_id) AS score_count,
                   COALESCE(SUM(CASE WHEN s.passed = 1 THEN 1 ELSE 0 END), 0) AS passed_count,
                   COALESCE(SUM(CASE WHEN s.passed = 0 THEN 1 ELSE 0 END), 0) AS failed_count
              FROM eval_runs e
              LEFT JOIN scores s ON s.eval_run_id = e.eval_run_id
             WHERE e.eval_run_id = ?
             GROUP BY e.eval_run_id
            "#,
            &[Bind::Text(eval_run_id.to_string())],
        )?;
        rows.into_iter().next().map(decode_eval_run).transpose()
    }

    pub fn list_eval_runs(
        &self,
        suite: Option<&str>,
        limit: i64,
    ) -> Result<Vec<EvalRunRecord>, String> {
        let rows = if let Some(suite) = suite {
            self.db.query(
                r#"
                SELECT e.*,
                       COUNT(s.score_id) AS score_count,
                       COALESCE(SUM(CASE WHEN s.passed = 1 THEN 1 ELSE 0 END), 0) AS passed_count,
                       COALESCE(SUM(CASE WHEN s.passed = 0 THEN 1 ELSE 0 END), 0) AS failed_count
                  FROM eval_runs e
                  LEFT JOIN scores s ON s.eval_run_id = e.eval_run_id
                 WHERE e.suite = ?
                 GROUP BY e.eval_run_id
                 ORDER BY COALESCE(e.ended_at, e.started_at) DESC, e.started_at DESC
                 LIMIT ?
                "#,
                &[Bind::Text(suite.to_string()), Bind::Int(limit)],
            )?
        } else {
            self.db.query(
                r#"
                SELECT e.*,
                       COUNT(s.score_id) AS score_count,
                       COALESCE(SUM(CASE WHEN s.passed = 1 THEN 1 ELSE 0 END), 0) AS passed_count,
                       COALESCE(SUM(CASE WHEN s.passed = 0 THEN 1 ELSE 0 END), 0) AS failed_count
                  FROM eval_runs e
                  LEFT JOIN scores s ON s.eval_run_id = e.eval_run_id
                 GROUP BY e.eval_run_id
                 ORDER BY COALESCE(e.ended_at, e.started_at) DESC, e.started_at DESC
                 LIMIT ?
                "#,
                &[Bind::Int(limit)],
            )?
        };
        rows.into_iter().map(decode_eval_run).collect()
    }

    pub fn export_data(&self) -> Result<Value, String> {
        let events = self
            .db
            .query("SELECT raw_json FROM events ORDER BY ts", &[])?
            .into_iter()
            .filter_map(|row| {
                row.get("raw_json")
                    .and_then(|text| serde_json::from_str::<Value>(text).ok())
            })
            .collect::<Vec<_>>();
        let runs = self
            .list_runs(10_000)?
            .into_iter()
            .map(|run| serde_json::to_value(run).unwrap_or(Value::Null))
            .collect::<Vec<_>>();
        let signals = self
            .list_signals(None, 10_000)?
            .into_iter()
            .map(|signal| serde_json::to_value(signal).unwrap_or(Value::Null))
            .collect::<Vec<_>>();
        let fixtures = self
            .list_fixtures(None)?
            .into_iter()
            .map(|fixture| serde_json::to_value(fixture).unwrap_or(Value::Null))
            .collect::<Vec<_>>();
        let eval_runs = self
            .list_eval_runs(None, 10_000)?
            .into_iter()
            .map(|eval_run| serde_json::to_value(eval_run).unwrap_or(Value::Null))
            .collect::<Vec<_>>();
        let score_rows = self
            .db
            .query("SELECT * FROM scores ORDER BY eval_run_id, fixture_id", &[])?
            .into_iter()
            .map(decode_score)
            .collect::<Result<Vec<_>, _>>()?
            .into_iter()
            .map(|score| serde_json::to_value(score).unwrap_or(Value::Null))
            .collect::<Vec<_>>();
        Ok(json!({
            "events": events,
            "runs": runs,
            "signals": signals,
            "fixtures": fixtures,
            "eval_runs": eval_runs,
            "scores": score_rows,
        }))
    }

    fn upsert_run(&self, event: &Value) -> Result<(), String> {
        let run_id = str_field(event, "run_id")?;
        let existing = self.db.query(
            "SELECT status FROM runs WHERE run_id = ?",
            &[Bind::Text(run_id.to_string())],
        )?;
        let current_status = existing
            .first()
            .and_then(|row| row.get("status").map(String::as_str));
        let status = derive_status(str_field(event, "event_type")?, current_status);
        let context = event.get("context").unwrap_or(&Value::Null);
        let message = event.get("message").and_then(Value::as_str);
        let first_prompt = if str_field(event, "event_type")? == "user_prompt" {
            message
        } else {
            None
        };
        let ended_at = if status == "completed" || status == "failed" {
            event.get("ts").and_then(Value::as_str)
        } else {
            None
        };
        if existing.is_empty() {
            self.db.execute(
                r#"
                INSERT INTO runs (
                  run_id, agent, repo, branch, started_at, ended_at, last_event_at,
                  status, produced_pr, checks_ran, first_prompt, latest_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                "#,
                &[
                    Bind::Text(run_id.to_string()),
                    bind_text(event, "agent"),
                    bind_opt_text(context.get("repo")),
                    bind_opt_text(context.get("branch")),
                    bind_text(event, "ts"),
                    bind_optional_str(ended_at),
                    bind_text(event, "ts"),
                    Bind::Text(status),
                    Bind::Int(if event_produced_pr(event) { 1 } else { 0 }),
                    Bind::Int(if event_ran_check(event) { 1 } else { 0 }),
                    bind_optional_str(first_prompt),
                    bind_optional_str(message),
                ],
            )
        } else {
            self.db.execute(
                r#"
                UPDATE runs
                   SET repo = COALESCE(repo, ?),
                       branch = COALESCE(branch, ?),
                       ended_at = CASE WHEN ? IS NOT NULL THEN ? ELSE ended_at END,
                       last_event_at = CASE WHEN ? > last_event_at THEN ? ELSE last_event_at END,
                       status = ?,
                       produced_pr = MAX(produced_pr, ?),
                       checks_ran = MAX(checks_ran, ?),
                       first_prompt = COALESCE(first_prompt, ?),
                       latest_message = COALESCE(?, latest_message)
                 WHERE run_id = ?
                "#,
                &[
                    bind_opt_text(context.get("repo")),
                    bind_opt_text(context.get("branch")),
                    bind_optional_str(ended_at),
                    bind_optional_str(ended_at),
                    bind_text(event, "ts"),
                    bind_text(event, "ts"),
                    Bind::Text(status),
                    Bind::Int(if event_produced_pr(event) { 1 } else { 0 }),
                    Bind::Int(if event_ran_check(event) { 1 } else { 0 }),
                    bind_optional_str(first_prompt),
                    bind_optional_str(message),
                    Bind::Text(run_id.to_string()),
                ],
            )
        }
    }

    fn refresh_run_rollups(&self, run_id: &str) -> Result<(), String> {
        let cost = scalar_f64(
            &self.db.query(
                "SELECT COALESCE(SUM(cost_usd_est), 0) AS value FROM events WHERE run_id = ?",
                &[Bind::Text(run_id.to_string())],
            )?,
            "value",
        );
        let tool_calls = scalar_i64(
            &self.db.query(
                "SELECT COUNT(*) AS value FROM events WHERE run_id = ? AND event_type IN ('post_tool','tool_failure','pre_tool')",
                &[Bind::Text(run_id.to_string())],
            )?,
            "value",
        );
        let signals = scalar_i64(
            &self.db.query(
                "SELECT COUNT(*) AS value FROM signals WHERE run_id = ? AND active = 1",
                &[Bind::Text(run_id.to_string())],
            )?,
            "value",
        );
        let files_touched = self.files_touched(run_id)?;
        self.db.execute(
            r#"
            UPDATE runs
               SET total_cost_usd_est = ?,
                   tool_calls = ?,
                   files_touched = ?,
                   signals_count = ?,
                   activity_json = ?
             WHERE run_id = ?
            "#,
            &[
                Bind::Float(cost),
                Bind::Int(tool_calls),
                Bind::Int(files_touched),
                Bind::Int(signals),
                Bind::Text("[]".to_string()),
                Bind::Text(run_id.to_string()),
            ],
        )
    }

    fn evaluate_run_signals(
        &self,
        run_id: &str,
        thresholds: &SignalThresholds,
    ) -> Result<(), String> {
        let events = self.get_run_events(run_id)?;
        self.detect_loop(run_id, &events, thresholds)?;
        self.detect_runaway_cost(run_id, thresholds)?;
        self.detect_skipped_checks(run_id, &events)?;
        self.detect_reread_thrash(run_id, &events, thresholds)?;
        self.detect_failure_cascade(run_id, &events, thresholds)?;
        Ok(())
    }

    fn detect_loop(
        &self,
        run_id: &str,
        events: &[Value],
        thresholds: &SignalThresholds,
    ) -> Result<(), String> {
        let mut counts: HashMap<String, (i64, String, Value)> = HashMap::new();
        for event in events {
            let event_type = event
                .get("event_type")
                .and_then(Value::as_str)
                .unwrap_or("");
            if !matches!(event_type, "post_tool" | "tool_failure" | "pre_tool") {
                continue;
            }
            let tool = event.get("tool").unwrap_or(&Value::Null);
            let name = tool
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let input = tool.get("input").cloned().unwrap_or(Value::Null);
            let fingerprint = format!(
                "{}:{}",
                name,
                serde_json::to_string(&input).unwrap_or_else(|_| "null".to_string())
            );
            let entry = counts
                .entry(fingerprint)
                .or_insert((0, name.to_string(), input));
            entry.0 += 1;
        }
        for (fingerprint, (count, tool, input)) in counts {
            if count >= thresholds.loop_repeats {
                self.add_signal(
                    run_id,
                    "loop",
                    "high",
                    json!({
                        "tool": tool,
                        "input": input,
                        "fingerprint": fingerprint,
                        "count": count,
                    }),
                    None,
                )?;
            }
        }
        Ok(())
    }

    fn detect_runaway_cost(
        &self,
        run_id: &str,
        thresholds: &SignalThresholds,
    ) -> Result<(), String> {
        let Some(run) = self.get_run(run_id)? else {
            return Ok(());
        };
        if run.total_cost_usd_est >= thresholds.runaway_cost_usd {
            self.add_signal(
                run_id,
                "runaway_cost",
                "high",
                json!({
                    "reason": "run_cost_over_budget",
                    "cost_usd_est": run.total_cost_usd_est,
                    "budget_usd": thresholds.runaway_cost_usd,
                    "fingerprint": "total",
                }),
                None,
            )?;
        }
        Ok(())
    }

    fn detect_skipped_checks(&self, run_id: &str, events: &[Value]) -> Result<(), String> {
        let Some(run) = self.get_run(run_id)? else {
            return Ok(());
        };
        if run.status != "completed" || !run.produced_pr || run.checks_ran {
            return Ok(());
        }
        if !events.iter().any(event_ran_check) {
            self.add_signal(
                run_id,
                "skipped_checks",
                "medium",
                json!({
                    "reason": "produced_pr_or_commit_without_test_or_build",
                    "fingerprint": "default",
                }),
                None,
            )?;
        }
        Ok(())
    }

    fn detect_reread_thrash(
        &self,
        run_id: &str,
        events: &[Value],
        thresholds: &SignalThresholds,
    ) -> Result<(), String> {
        let mut reads: HashMap<String, i64> = HashMap::new();
        for event in events {
            let tool = event.get("tool").unwrap_or(&Value::Null);
            if tool
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .eq_ignore_ascii_case("read")
            {
                for path in extract_paths(event) {
                    *reads.entry(path).or_default() += 1;
                }
            }
        }
        for (path, count) in reads {
            if count >= thresholds.reread_repeats {
                self.add_signal(
                    run_id,
                    "reread_thrash",
                    "low",
                    json!({
                        "path": path,
                        "read_count": count,
                        "threshold": thresholds.reread_repeats,
                    }),
                    None,
                )?;
            }
        }
        Ok(())
    }

    fn detect_failure_cascade(
        &self,
        run_id: &str,
        events: &[Value],
        thresholds: &SignalThresholds,
    ) -> Result<(), String> {
        let recent = events.iter().rev().take(10).collect::<Vec<_>>();
        let failures = recent
            .iter()
            .filter(|event| event.get("event_type").and_then(Value::as_str) == Some("tool_failure"))
            .count() as i64;
        if failures >= thresholds.failure_cascade_count {
            self.add_signal(
                run_id,
                "failure_cascade",
                "high",
                json!({
                    "failures_in_recent_events": failures,
                    "window": recent.len(),
                    "fingerprint": "recent_failures",
                }),
                None,
            )?;
        }
        Ok(())
    }

    fn enrich_run(&self, mut run: RunSummary) -> Result<RunSummary, String> {
        let rollup = self.subagent_rollup(&run.run_id)?;
        run.subagents_count = rollup.subagents_count;
        run.max_depth = rollup.max_depth;
        run.labels = self.run_labels(&run.run_id)?;
        Ok(run)
    }

    fn persistable_event(&self, event: &Value) -> Value {
        if self.raw_payloads {
            return event.clone();
        }
        let mut cleaned = event.clone();
        if let Some(object) = cleaned.as_object_mut() {
            object.insert("raw".to_string(), json!({}));
        }
        cleaned
    }

    fn files_touched(&self, run_id: &str) -> Result<i64, String> {
        let paths = self
            .get_run_events(run_id)?
            .iter()
            .flat_map(extract_paths)
            .collect::<BTreeSet<_>>();
        Ok(paths.len() as i64)
    }

    fn delete_count(&self, table: &str, where_clause: Option<&str>) -> Result<i64, String> {
        let before = scalar_i64(
            &self
                .db
                .query(&format!("SELECT COUNT(*) AS value FROM {table}"), &[])?,
            "value",
        );
        let sql = match where_clause {
            Some(where_clause) => format!("DELETE FROM {table} WHERE {where_clause}"),
            None => format!("DELETE FROM {table}"),
        };
        self.db.execute(&sql, &[])?;
        let after = scalar_i64(
            &self
                .db
                .query(&format!("SELECT COUNT(*) AS value FROM {table}"), &[])?,
            "value",
        );
        Ok(before - after)
    }

    fn delete_run_rows(&self, table: &str, run_id: &str) -> Result<i64, String> {
        let before = scalar_i64(
            &self.db.query(
                &format!("SELECT COUNT(*) AS value FROM {table} WHERE run_id = ?"),
                &[Bind::Text(run_id.to_string())],
            )?,
            "value",
        );
        self.db.execute(
            &format!("DELETE FROM {table} WHERE run_id = ?"),
            &[Bind::Text(run_id.to_string())],
        )?;
        let after = scalar_i64(
            &self.db.query(
                &format!("SELECT COUNT(*) AS value FROM {table} WHERE run_id = ?"),
                &[Bind::Text(run_id.to_string())],
            )?,
            "value",
        );
        Ok(before - after)
    }
}

fn derive_status(event_type: &str, current: Option<&str>) -> String {
    if event_type == "tool_failure" {
        return "failed".to_string();
    }
    if event_type == "permission_request" || event_type == "permission_denied" {
        return "waiting".to_string();
    }
    if ["session_end", "stop", "task_completed"].contains(&event_type) {
        return if current == Some("failed") {
            "failed"
        } else {
            "completed"
        }
        .to_string();
    }
    if matches!(current, Some("completed" | "failed"))
        && event_type != "session_start"
        && event_type != "user_prompt"
    {
        return current.unwrap().to_string();
    }
    "running".to_string()
}

fn str_field<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing field: {key}"))
}

fn bind_text(value: &Value, key: &str) -> Bind {
    bind_opt_text(value.get(key))
}

fn bind_opt_text(value: Option<&Value>) -> Bind {
    match value {
        Some(Value::String(text)) if !text.is_empty() => Bind::Text(text.clone()),
        Some(Value::Number(number)) => Bind::Text(number.to_string()),
        _ => Bind::Null,
    }
}

fn bind_optional_str(value: Option<&str>) -> Bind {
    value
        .map(|text| Bind::Text(text.to_string()))
        .unwrap_or(Bind::Null)
}

fn bind_opt_i64(value: Option<&Value>) -> Bind {
    value
        .and_then(Value::as_i64)
        .map(Bind::Int)
        .unwrap_or(Bind::Null)
}

fn bind_opt_f64(value: Option<&Value>) -> Bind {
    value
        .and_then(Value::as_f64)
        .map(Bind::Float)
        .unwrap_or(Bind::Null)
}

fn scalar_i64(rows: &[std::collections::BTreeMap<String, String>], key: &str) -> i64 {
    rows.first()
        .and_then(|row| row.get(key))
        .and_then(|text| text.parse().ok())
        .unwrap_or(0)
}

fn scalar_f64(rows: &[std::collections::BTreeMap<String, String>], key: &str) -> f64 {
    rows.first()
        .and_then(|row| row.get(key))
        .and_then(|text| text.parse().ok())
        .unwrap_or(0.0)
}

fn decode_signal(row: BTreeMap<String, String>) -> Result<SignalRecord, String> {
    Ok(SignalRecord {
        signal_id: required(&row, "signal_id")?,
        signal_key: required(&row, "signal_key")?,
        run_id: required(&row, "run_id")?,
        signal_type: required(&row, "type")?,
        severity: required(&row, "severity")?,
        fired_at: required(&row, "fired_at")?,
        evidence: optional(&row, "evidence_json")
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| json!({})),
        action: optional(&row, "action"),
        active: optional(&row, "active").is_some_and(|text| text == "1"),
    })
}

fn decode_fixture(row: BTreeMap<String, String>) -> Result<FixtureRecord, String> {
    Ok(FixtureRecord {
        fixture_id: required(&row, "fixture_id")?,
        run_id: required(&row, "run_id")?,
        suite: required(&row, "suite")?,
        prompt: optional(&row, "prompt"),
        repo_ref: optional(&row, "repo_ref_json")
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| json!({})),
        recorded_trajectory: optional(&row, "recorded_trajectory_json")
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_default(),
        created_at: required(&row, "created_at")?,
    })
}

fn decode_eval_run(row: BTreeMap<String, String>) -> Result<EvalRunRecord, String> {
    Ok(EvalRunRecord {
        eval_run_id: required(&row, "eval_run_id")?,
        suite: required(&row, "suite")?,
        baseline_eval_run_id: optional(&row, "baseline_eval_run_id"),
        started_at: required(&row, "started_at")?,
        ended_at: optional(&row, "ended_at"),
        status: required(&row, "status")?,
        score_count: optional(&row, "score_count")
            .and_then(|text| text.parse().ok())
            .unwrap_or(0),
        passed_count: optional(&row, "passed_count")
            .and_then(|text| text.parse().ok())
            .unwrap_or(0),
        failed_count: optional(&row, "failed_count")
            .and_then(|text| text.parse().ok())
            .unwrap_or(0),
    })
}

fn decode_score(row: BTreeMap<String, String>) -> Result<ScoreRecord, String> {
    Ok(ScoreRecord {
        score_id: required(&row, "score_id")?,
        eval_run_id: required(&row, "eval_run_id")?,
        fixture_id: required(&row, "fixture_id")?,
        scorer: required(&row, "scorer")?,
        value: optional(&row, "value").and_then(|text| text.parse().ok()),
        passed: optional(&row, "passed").is_some_and(|text| text == "1"),
        detail: optional(&row, "detail_json")
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| json!({})),
    })
}

fn decode_run_score(row: BTreeMap<String, String>) -> Result<RunScoreRecord, String> {
    let score = decode_score(row.clone())?;
    Ok(RunScoreRecord {
        score_id: score.score_id,
        eval_run_id: score.eval_run_id,
        fixture_id: score.fixture_id,
        scorer: score.scorer,
        value: score.value,
        passed: score.passed,
        detail: score.detail,
        suite: required(&row, "suite")?,
        eval_status: required(&row, "eval_status")?,
        eval_started_at: required(&row, "eval_started_at")?,
        eval_ended_at: optional(&row, "eval_ended_at"),
    })
}

fn decode_run(row: std::collections::BTreeMap<String, String>) -> Result<RunSummary, String> {
    Ok(RunSummary {
        run_id: required(&row, "run_id")?,
        agent: required(&row, "agent")?,
        repo: optional(&row, "repo"),
        branch: optional(&row, "branch"),
        started_at: required(&row, "started_at")?,
        ended_at: optional(&row, "ended_at"),
        last_event_at: required(&row, "last_event_at")?,
        status: required(&row, "status")?,
        total_cost_usd_est: optional(&row, "total_cost_usd_est")
            .and_then(|text| text.parse().ok())
            .unwrap_or(0.0),
        tool_calls: optional(&row, "tool_calls")
            .and_then(|text| text.parse().ok())
            .unwrap_or(0),
        files_touched: optional(&row, "files_touched")
            .and_then(|text| text.parse().ok())
            .unwrap_or(0),
        produced_pr: optional(&row, "produced_pr").is_some_and(|text| text == "1"),
        checks_ran: optional(&row, "checks_ran").is_some_and(|text| text == "1"),
        signals_count: optional(&row, "signals_count")
            .and_then(|text| text.parse().ok())
            .unwrap_or(0),
        first_prompt: optional(&row, "first_prompt"),
        latest_message: optional(&row, "latest_message"),
        labels: BTreeMap::new(),
        subagents_count: 0,
        max_depth: 0,
    })
}

fn required(row: &std::collections::BTreeMap<String, String>, key: &str) -> Result<String, String> {
    optional(row, key).ok_or_else(|| format!("missing column: {key}"))
}

fn optional(row: &std::collections::BTreeMap<String, String>, key: &str) -> Option<String> {
    row.get(key).filter(|value| !value.is_empty()).cloned()
}

fn first_user_prompt(events: &[Value]) -> Option<String> {
    events.iter().find_map(|event| {
        if event.get("event_type").and_then(Value::as_str) == Some("user_prompt") {
            event
                .get("message")
                .and_then(Value::as_str)
                .map(ToString::to_string)
        } else {
            None
        }
    })
}

fn first_event_cwd(events: &[Value]) -> Option<String> {
    events.iter().find_map(|event| {
        event
            .get("context")
            .and_then(|context| context.get("cwd"))
            .and_then(Value::as_str)
            .filter(|cwd| !cwd.is_empty())
            .map(ToString::to_string)
    })
}

fn merge_object(target: &mut Value, extra: Option<Value>) {
    let Some(Value::Object(extra)) = extra else {
        return;
    };
    if !target.is_object() {
        *target = json!({});
    }
    if let Some(target) = target.as_object_mut() {
        for (key, value) in extra {
            target.insert(key, value);
        }
    }
}

fn normalize_label_filters(labels: &[String]) -> Vec<String> {
    labels
        .iter()
        .map(|label| label.trim())
        .filter(|label| !label.is_empty())
        .map(ToString::to_string)
        .collect()
}

fn labels_match(labels: &BTreeMap<String, Vec<String>>, filters: &[String]) -> bool {
    filters.iter().all(|filter| {
        if let Some((key, value)) = filter.split_once('=') {
            labels
                .get(key)
                .is_some_and(|values| values.iter().any(|existing| existing == value))
        } else {
            labels.contains_key(filter)
        }
    })
}

fn label_value_to_string(value: &Value) -> String {
    match value {
        Value::String(text) => text.to_string(),
        Value::Number(number) => number.to_string(),
        Value::Bool(value) => value.to_string(),
        Value::Null => "null".to_string(),
        _ => serde_json::to_string(value).unwrap_or_else(|_| value.to_string()),
    }
}

fn should_sample_run(run_id: &str, sample_rate: f64) -> bool {
    let sample_rate = sample_rate.clamp(0.0, 1.0);
    if sample_rate <= 0.0 {
        return false;
    }
    if sample_rate >= 1.0 {
        return true;
    }
    let mut hash: u64 = 0xcbf29ce484222325;
    for byte in run_id.as_bytes() {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    let bucket = hash as f64 / u64::MAX as f64;
    bucket <= sample_rate
}

pub fn extract_paths(event: &Value) -> BTreeSet<String> {
    let mut paths = BTreeSet::new();
    for source in [Some(event), event.get("raw")].into_iter().flatten() {
        for key in ["file_path", "filePath", "path", "filename"] {
            if let Some(path) = source
                .get(key)
                .and_then(Value::as_str)
                .filter(|path| !path.is_empty())
            {
                paths.insert(path.to_string());
            }
        }
    }
    let tool = event.get("tool").unwrap_or(&Value::Null);
    let input = tool.get("input").unwrap_or(&Value::Null);
    if input.is_object() {
        for key in ["file_path", "filePath", "path", "filename"] {
            if let Some(path) = input
                .get(key)
                .and_then(Value::as_str)
                .filter(|path| !path.is_empty())
            {
                paths.insert(path.to_string());
            }
        }
    }
    paths
}

fn file_access_kind(event: &Value) -> &'static str {
    if event.get("event_type").and_then(Value::as_str) == Some("file_changed") {
        return "write";
    }
    let name = event
        .get("tool")
        .and_then(|tool| tool.get("name"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    match name.as_str() {
        "write"
        | "edit"
        | "multiedit"
        | "notebookedit"
        | "apply_patch"
        | "str_replace_editor"
        | "str_replace_based_edit_tool" => "write",
        _ => "read",
    }
}

pub fn extract_tool_command(event: &Value) -> Option<String> {
    let tool = event.get("tool")?;
    let input = tool.get("input")?;
    if let Some(text) = input.as_str() {
        return Some(text.to_string());
    }
    for key in ["command", "cmd", "script"] {
        if let Some(text) = input.get(key).and_then(Value::as_str) {
            return Some(text.to_string());
        }
    }
    None
}

fn event_diff_preview(event: &Value, max_chars: usize) -> Option<Value> {
    let tool = event.get("tool")?;
    let input = tool.get("input").filter(|value| value.is_object())?;
    let name = tool
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    let path = first_path_from_mapping(input);
    if matches!(
        name.as_str(),
        "edit" | "str_replace_editor" | "str_replace_based_edit_tool"
    ) {
        let old = first_string(
            input,
            &["old_string", "oldString", "old_str", "oldStr", "old"],
        );
        let new = first_string(
            input,
            &["new_string", "newString", "new_str", "newStr", "new"],
        );
        if let (Some(old), Some(new)) = (old, new) {
            let text = unified_text_diff(&old, &new, path.as_deref(), None);
            let (text, truncated) = trim_text(text, max_chars);
            return Some(
                json!({"kind": "edit", "path": path, "text": text, "truncated": truncated}),
            );
        }
    }
    if name == "multiedit" {
        let mut parts = Vec::new();
        if let Some(edits) = input.get("edits").and_then(Value::as_array) {
            for (index, edit) in edits.iter().enumerate() {
                if !edit.is_object() {
                    continue;
                }
                let old = first_string(
                    edit,
                    &["old_string", "oldString", "old_str", "oldStr", "old"],
                );
                let new = first_string(
                    edit,
                    &["new_string", "newString", "new_str", "newStr", "new"],
                );
                if let (Some(old), Some(new)) = (old, new) {
                    parts.push(format!(
                        "# edit {}\n{}",
                        index + 1,
                        unified_text_diff(&old, &new, path.as_deref(), None)
                    ));
                }
            }
        }
        if !parts.is_empty() {
            let (text, truncated) = trim_text(parts.join("\n"), max_chars);
            return Some(
                json!({"kind": "edit", "path": path, "text": text, "truncated": truncated}),
            );
        }
    }
    if name == "write" {
        if let Some(content) = first_string(input, &["content", "text"]) {
            let text = unified_text_diff("", &content, path.as_deref(), Some("/dev/null"));
            let (text, truncated) = trim_text(text, max_chars);
            return Some(
                json!({"kind": "write", "path": path, "text": text, "truncated": truncated}),
            );
        }
    }
    if name == "apply_patch" {
        if let Some(patch) = first_string(input, &["patch", "diff"]).filter(|text| !text.is_empty())
        {
            let (text, truncated) = trim_text(patch, max_chars);
            return Some(
                json!({"kind": "patch", "path": path, "text": text, "truncated": truncated}),
            );
        }
    }
    None
}

fn unified_text_diff(old: &str, new: &str, path: Option<&str>, fromfile: Option<&str>) -> String {
    let label = path.unwrap_or("unknown");
    let mut lines = vec![
        format!("--- {}", fromfile.unwrap_or(&format!("a/{label}"))),
        format!("+++ b/{label}"),
        "@@ -1 +1 @@".to_string(),
    ];
    lines.extend(old.lines().map(|line| format!("-{line}")));
    lines.extend(new.lines().map(|line| format!("+{line}")));
    lines.join("\n")
}

fn first_string(mapping: &Value, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| {
        mapping
            .get(*key)
            .and_then(Value::as_str)
            .map(ToString::to_string)
    })
}

fn first_path_from_mapping(mapping: &Value) -> Option<String> {
    for key in ["file_path", "filePath", "path", "filename"] {
        if let Some(path) = mapping
            .get(key)
            .and_then(Value::as_str)
            .filter(|path| !path.is_empty())
        {
            return Some(path.to_string());
        }
    }
    None
}

fn trim_text(text: String, max_chars: usize) -> (String, bool) {
    if text.chars().count() <= max_chars {
        return (text, false);
    }
    let mut trimmed = text.chars().take(max_chars).collect::<String>();
    trimmed.push_str("\n... truncated ...");
    (trimmed, true)
}

fn tool_or_event_type(event: &Value) -> String {
    event
        .get("tool")
        .and_then(|tool| tool.get("name"))
        .and_then(Value::as_str)
        .or_else(|| event.get("event_type").and_then(Value::as_str))
        .unwrap_or("event")
        .to_string()
}

fn event_ran_check(event: &Value) -> bool {
    extract_tool_command(event).is_some_and(|command| command_looks_like_check(&command))
}

fn event_produced_pr(event: &Value) -> bool {
    if extract_tool_command(event).is_some_and(|command| command_looks_like_pr_or_commit(&command))
    {
        return true;
    }
    event
        .get("message")
        .and_then(Value::as_str)
        .is_some_and(|message| {
            let lowered = message.to_ascii_lowercase();
            lowered.contains("pull request") || lowered.contains("pr #")
        })
}

fn command_looks_like_check(command: &str) -> bool {
    command_looks_like_test(command)
        || command_looks_like_build(command)
        || contains_any(
            command,
            &[
                "ruff",
                "eslint",
                "flake8",
                "pylint",
                "npm run lint",
                "pnpm lint",
                "pnpm run lint",
                "yarn lint",
                "yarn run lint",
                "go vet",
            ],
        )
}

fn command_looks_like_test(command: &str) -> bool {
    contains_any(
        command,
        &[
            "pytest",
            "unittest",
            "vitest",
            "jest",
            "cargo test",
            "go test",
            "make test",
            "npm test",
            "npm run test",
            "pnpm test",
            "pnpm run test",
            "yarn test",
            "yarn run test",
            "bun test",
            "bun run test",
            "tox",
            "nox",
            "rspec",
            "phpunit",
            "mvn test",
            "gradle test",
            "./gradlew test",
        ],
    )
}

fn command_looks_like_build(command: &str) -> bool {
    contains_any(
        command,
        &[
            "npm run build",
            "pnpm build",
            "pnpm run build",
            "yarn build",
            "yarn run build",
            "bun run build",
            "cargo build",
            "go build",
            "make build",
            "cmake --build",
            "mvn package",
            "gradle build",
            "./gradlew build",
            "typecheck",
            "tsc",
            "mypy",
        ],
    )
}

fn command_looks_like_pr_or_commit(command: &str) -> bool {
    contains_any(
        command,
        &["gh pr create", "git commit", "git push", "hub pull-request"],
    )
}

fn contains_any(command: &str, terms: &[&str]) -> bool {
    let lowered = command.to_ascii_lowercase();
    terms.iter().any(|term| lowered.contains(term))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::normalize::normalize_event;

    #[test]
    fn records_and_rolls_up_run() {
        let path =
            std::env::temp_dir().join(format!("tranquil-rust-{}.db", crate::util::now_millis()));
        let storage = Storage::open(&path).unwrap();
        let event = normalize_event(
            "PostToolUse",
            &json!({
                "agent": "codex",
                "session_id": "rust-storage",
                "repo": "api",
                "branch": "main",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest"},
                "usage": {"cost_usd": 0.25}
            }),
            "hook",
        );
        storage.record_event(&event).unwrap();
        let runs = storage.list_runs(10).unwrap();
        assert_eq!(runs.len(), 1);
        assert_eq!(runs[0].agent, "codex");
        assert_eq!(runs[0].tool_calls, 1);
        assert_eq!(runs[0].total_cost_usd_est, 0.25);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn raw_payloads_false_drops_vendor_raw_payload() {
        let path = std::env::temp_dir().join(format!(
            "tranquil-rust-raw-{}.db",
            crate::util::now_millis()
        ));
        let storage = Storage::open_with_raw_payloads(&path, false).unwrap();
        let event = normalize_event(
            "UserPromptSubmit",
            &json!({
                "session_id": "rust-raw",
                "prompt": "hide raw",
                "secret_payload": "should-not-persist-under-raw"
            }),
            "hook",
        );
        let run_id = event["run_id"].as_str().unwrap().to_string();
        storage.record_event(&event).unwrap();
        let events = storage.get_run_events(&run_id).unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["raw"], json!({}));
        assert_eq!(events[0]["message"], "hide raw");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn list_runs_filters_by_agent_repo_branch_status_and_labels() {
        let path = std::env::temp_dir().join(format!(
            "tranquil-rust-filter-{}.db",
            crate::util::now_millis()
        ));
        let storage = Storage::open(&path).unwrap();
        for (event_hint, prompt) in [
            ("UserPromptSubmit", "ship the api"),
            ("TaskCompleted", "done"),
        ] {
            let event = normalize_event(
                event_hint,
                &json!({
                    "run_id": "run_filter_one",
                    "session_id": "filter-main",
                    "agent": "codex",
                    "repo": "api",
                    "branch": "main",
                    "prompt": prompt,
                    "labels": {"team": "alpha", "priority": "p1"}
                }),
                "hook",
            );
            storage.record_event(&event).unwrap();
        }
        let runs = storage
            .list_runs_filtered(&RunFilters {
                limit: 10,
                status: Some("completed".to_string()),
                agent: Some("codex".to_string()),
                repo: Some("api".to_string()),
                branch: Some("main".to_string()),
                labels: vec!["team=alpha".to_string(), "priority".to_string()],
                since: None,
            })
            .unwrap();
        assert_eq!(runs.len(), 1);
        assert_eq!(runs[0].run_id, "run_filter_one");
        assert_eq!(
            runs[0].labels.get("team").unwrap(),
            &vec!["alpha".to_string()]
        );
        let no_match = storage
            .list_runs_filtered(&RunFilters {
                limit: 10,
                labels: vec!["team=beta".to_string()],
                ..RunFilters::default()
            })
            .unwrap();
        assert!(no_match.is_empty());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn run_detail_helpers_report_subagents_files_and_diffs() {
        let path = std::env::temp_dir().join(format!(
            "tranquil-rust-detail-{}.db",
            crate::util::now_millis()
        ));
        let storage = Storage::open(&path).unwrap();
        let run_id = "run_detail_one";
        let events = [
            normalize_event(
                "UserPromptSubmit",
                &json!({
                    "run_id": run_id,
                    "session_id": "detail-main",
                    "prompt": "inspect files"
                }),
                "hook",
            ),
            normalize_event(
                "PostToolUse",
                &json!({
                    "run_id": run_id,
                    "session_id": "detail-child",
                    "parent_session_id": "detail-main",
                    "depth": 1,
                    "tool_name": "Read",
                    "tool_input": {"file_path": "src/lib.rs"}
                }),
                "hook",
            ),
            normalize_event(
                "PostToolUse",
                &json!({
                    "run_id": run_id,
                    "session_id": "detail-main",
                    "tool_name": "Edit",
                    "tool_input": {
                        "file_path": "src/lib.rs",
                        "old_string": "old",
                        "new_string": "new"
                    }
                }),
                "hook",
            ),
        ];
        for event in events {
            storage.record_event(&event).unwrap();
        }
        let run = storage.get_run(run_id).unwrap().unwrap();
        assert_eq!(run.subagents_count, 1);
        assert_eq!(run.max_depth, 1);
        let subagents = storage.list_subagents(run_id).unwrap();
        assert_eq!(subagents.len(), 1);
        assert_eq!(subagents[0].session_id, "detail-child");
        let files = storage.file_touch_summary(run_id).unwrap();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].reads, 1);
        assert_eq!(files[0].writes, 1);
        let display_events = storage.get_run_display_events(run_id).unwrap();
        assert!(
            display_events
                .iter()
                .any(|event| event.get("diff").is_some())
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn detects_loop_signal_and_stop_request() {
        let path = std::env::temp_dir().join(format!(
            "tranquil-rust-signal-{}.db",
            crate::util::now_millis()
        ));
        let storage = Storage::open(&path).unwrap();
        let thresholds = SignalThresholds::default();
        let mut run_id = String::new();
        for index in 0..3 {
            let event = normalize_event(
                "PostToolUse",
                &json!({
                    "event_id": format!("evt_loop_{index}"),
                    "session_id": "rust-loop",
                    "tool_name": "Bash",
                    "tool_input": {"command": "npm test"}
                }),
                "hook",
            );
            run_id = event["run_id"].as_str().unwrap().to_string();
            storage
                .record_event_with_thresholds(&event, &thresholds)
                .unwrap();
        }
        let signals = storage.list_signals(Some(true), 20).unwrap();
        assert!(signals.iter().any(|signal| signal.signal_type == "loop"));
        assert!(storage.request_stop(&run_id, "test_stop").unwrap());
        assert!(
            storage
                .has_active_signal(&run_id, "stop_requested")
                .unwrap()
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn retention_purge_preserves_fixture_backed_runs() {
        let path = std::env::temp_dir().join(format!(
            "tranquil-rust-retention-{}.db",
            crate::util::now_millis()
        ));
        let storage = Storage::open(&path).unwrap();
        let old_ts = (crate::util::now_millis() - 3 * 86_400_000).to_string();
        let fixture_event = normalize_event(
            "TaskCompleted",
            &json!({
                "run_id": "run_old_fixture",
                "session_id": "old-fixture",
                "ts": old_ts,
                "prompt": "keep me"
            }),
            "hook",
        );
        storage.record_event(&fixture_event).unwrap();
        storage
            .create_fixture("run_old_fixture", "retention", None)
            .unwrap();
        let old_event = normalize_event(
            "TaskCompleted",
            &json!({
                "run_id": "run_old_plain",
                "session_id": "old-plain",
                "ts": old_ts,
                "prompt": "drop me"
            }),
            "hook",
        );
        storage.record_event(&old_event).unwrap();
        let current_seconds_event = normalize_event(
            "TaskCompleted",
            &json!({
                "run_id": "run_current_seconds",
                "session_id": "current-seconds",
                "ts": ((crate::util::now_millis() as f64) / 1000.0),
                "prompt": "keep current seconds"
            }),
            "hook",
        );
        storage.record_event(&current_seconds_event).unwrap();
        let counts = storage.purge_older_than(1).unwrap();
        assert_eq!(counts.events, 2);
        assert_eq!(counts.runs, 1);
        assert!(storage.get_run("run_old_fixture").unwrap().is_some());
        assert!(storage.get_run("run_old_plain").unwrap().is_none());
        assert!(storage.get_run("run_current_seconds").unwrap().is_some());
        assert_eq!(storage.get_run_events("run_old_fixture").unwrap().len(), 0);
        assert_eq!(storage.list_fixtures(Some("retention")).unwrap().len(), 1);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn purge_all_deletes_runtime_tables() {
        let path = std::env::temp_dir().join(format!(
            "tranquil-rust-purge-{}.db",
            crate::util::now_millis()
        ));
        let storage = Storage::open(&path).unwrap();
        let event = normalize_event(
            "UserPromptSubmit",
            &json!({"session_id": "rust-purge", "prompt": "clean"}),
            "hook",
        );
        storage.record_event(&event).unwrap();
        let counts = storage.purge(true).unwrap();
        assert_eq!(counts.runs, 1);
        assert_eq!(counts.events, 1);
        assert_eq!(storage.stats().unwrap().runs, 0);
        let _ = std::fs::remove_file(path);
    }
}
