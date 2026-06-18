use std::io::{self, BufRead, Write};

use serde_json::{Value, json};

use crate::storage::{RunFilters, Storage};

pub fn run_mcp_server(storage: &Storage) -> Result<i32, String> {
    let stdin = io::stdin();
    let mut stdout = io::stdout();
    for line in stdin.lock().lines() {
        let line = line.map_err(|err| err.to_string())?;
        if line.trim().is_empty() {
            continue;
        }
        let response = match serde_json::from_str::<Value>(&line) {
            Ok(request) => handle_request(storage, &request),
            Err(err) => Some(error_response(
                Value::Null,
                -32700,
                &format!("ParseError: {err}"),
            )),
        };
        if let Some(response) = response {
            writeln!(
                stdout,
                "{}",
                serde_json::to_string(&response).map_err(|err| err.to_string())?
            )
            .map_err(|err| err.to_string())?;
            stdout.flush().map_err(|err| err.to_string())?;
        }
    }
    Ok(0)
}

fn handle_request(storage: &Storage, request: &Value) -> Option<Value> {
    let id = request.get("id").cloned().unwrap_or(Value::Null);
    let method = request.get("method").and_then(Value::as_str).unwrap_or("");
    let params = request.get("params").cloned().unwrap_or_else(|| json!({}));
    if id.is_null() && method.starts_with("notifications/") {
        return None;
    }
    Some(match method {
        "initialize" => result_response(
            id,
            json!({
                "protocolVersion": params.get("protocolVersion").and_then(Value::as_str).unwrap_or("2024-11-05"),
                "serverInfo": {"name": "tranquil", "version": "0.1.0", "runtime": "rust"},
                "capabilities": {"tools": {}},
            }),
        ),
        "tools/list" => result_response(id, json!({ "tools": tool_definitions() })),
        "tools/call" => {
            let name = params.get("name").and_then(Value::as_str).unwrap_or("");
            let args = params
                .get("arguments")
                .cloned()
                .unwrap_or_else(|| json!({}));
            match call_tool(storage, name, &args) {
                Ok(result) => result_response(
                    id,
                    json!({
                        "content": [{"type": "text", "text": serde_json::to_string_pretty(&result).unwrap_or_else(|_| "null".to_string())}],
                        "isError": false,
                    }),
                ),
                Err(err) => error_response(id, -32602, &err),
            }
        }
        "ping" => result_response(id, json!({})),
        _ => error_response(id, -32601, &format!("unknown method: {method}")),
    })
}

fn call_tool(storage: &Storage, name: &str, args: &Value) -> Result<Value, String> {
    match name {
        "tranquil_query_runs" => {
            let filters = run_filters_from_args(args);
            Ok(serde_json::to_value(storage.list_runs_filtered(&filters)?)
                .map_err(|err| err.to_string())?)
        }
        "tranquil_get_run" => {
            let run_id = required(args, "run_id")?;
            let run = storage
                .get_run(run_id)?
                .ok_or_else(|| format!("run not found: {run_id}"))?;
            Ok(json!({
                "run": run,
                "events": storage.get_run_events(run_id)?,
                "signals": storage.list_run_signals(run_id)?,
            }))
        }
        "tranquil_cost" => {
            let group_by = args
                .get("group_by")
                .and_then(Value::as_str)
                .unwrap_or("agent");
            Ok(serde_json::to_value(storage.cost_rollup(group_by)?)
                .map_err(|err| err.to_string())?)
        }
        "tranquil_signals" => {
            let active = args.get("active").and_then(Value::as_bool).unwrap_or(true);
            Ok(
                serde_json::to_value(storage.list_signals(Some(active), 100)?)
                    .map_err(|err| err.to_string())?,
            )
        }
        "tranquil_eval_status" => {
            let suite = args.get("suite").and_then(Value::as_str);
            let eval_runs = storage.list_eval_runs(suite, 10)?;
            let mut scores = serde_json::Map::new();
            for eval_run in &eval_runs {
                scores.insert(
                    eval_run.eval_run_id.clone(),
                    serde_json::to_value(storage.list_scores(&eval_run.eval_run_id)?)
                        .map_err(|err| err.to_string())?,
                );
            }
            Ok(json!({"eval_runs": eval_runs, "scores": scores}))
        }
        "tranquil_diff_runs" => {
            let a = required(args, "a")?;
            let b = required(args, "b")?;
            Ok(serde_json::to_value(storage.diff_runs(a, b)?).map_err(|err| err.to_string())?)
        }
        other => Err(format!("unknown tool: {other}")),
    }
}

fn tool_definitions() -> Vec<Value> {
    vec![
        json!({
            "name": "tranquil_query_runs",
            "description": "Return recent Tranquil run summaries.",
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}, "status": {"type": "string"}, "agent": {"type": "string"}, "repo": {"type": "string"}, "branch": {"type": "string"}, "since": {"type": "string"}, "label": {"type": "string"}, "labels": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}}},
        }),
        json!({
            "name": "tranquil_get_run",
            "description": "Return a run summary, normalized transcript events, and signals.",
            "inputSchema": {"type": "object", "required": ["run_id"], "properties": {"run_id": {"type": "string"}}},
        }),
        json!({
            "name": "tranquil_cost",
            "description": "Return estimated cost rollups grouped by agent, repo, branch, or status.",
            "inputSchema": {"type": "object", "properties": {"group_by": {"type": "string", "enum": ["agent", "repo", "branch", "status"]}}},
        }),
        json!({
            "name": "tranquil_signals",
            "description": "Return active or all Tranquil signals.",
            "inputSchema": {"type": "object", "properties": {"active": {"type": "boolean"}}},
        }),
        json!({
            "name": "tranquil_eval_status",
            "description": "Return recent eval runs and scores.",
            "inputSchema": {"type": "object", "properties": {"suite": {"type": "string"}}},
        }),
        json!({
            "name": "tranquil_diff_runs",
            "description": "Compare two run summaries and tool timelines.",
            "inputSchema": {"type": "object", "required": ["a", "b"], "properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
        }),
    ]
}

fn run_filters_from_args(args: &Value) -> RunFilters {
    let mut labels = Vec::new();
    if let Some(label) = args.get("label").and_then(Value::as_str) {
        labels.push(label.to_string());
    }
    if let Some(value) = args.get("labels") {
        if let Some(label) = value.as_str() {
            labels.extend(
                label
                    .split(',')
                    .map(str::trim)
                    .filter(|label| !label.is_empty())
                    .map(ToString::to_string),
            );
        } else if let Some(items) = value.as_array() {
            labels.extend(
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::trim)
                    .filter(|label| !label.is_empty())
                    .map(ToString::to_string),
            );
        }
    }
    RunFilters {
        limit: args.get("limit").and_then(Value::as_i64).unwrap_or(20),
        status: args
            .get("status")
            .and_then(Value::as_str)
            .map(ToString::to_string),
        agent: args
            .get("agent")
            .and_then(Value::as_str)
            .map(ToString::to_string),
        repo: args
            .get("repo")
            .and_then(Value::as_str)
            .map(ToString::to_string),
        branch: args
            .get("branch")
            .and_then(Value::as_str)
            .map(ToString::to_string),
        labels,
        since: args
            .get("since")
            .and_then(Value::as_str)
            .map(ToString::to_string),
    }
}

fn required<'a>(args: &'a Value, key: &str) -> Result<&'a str, String> {
    args.get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("missing required argument: {key}"))
}

fn result_response(id: Value, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

fn error_response(id: Value, code: i64, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::normalize::normalize_event;
    use crate::storage::Storage;

    #[test]
    fn query_runs_tool_returns_captured_run() {
        let path = std::env::temp_dir().join(format!(
            "tranquil-rust-mcp-{}.db",
            crate::util::now_millis()
        ));
        let storage = Storage::open(&path).unwrap();
        let event = normalize_event(
            "UserPromptSubmit",
            &json!({"session_id": "mcp", "prompt": "status"}),
            "hook",
        );
        storage.record_event(&event).unwrap();
        let result = call_tool(&storage, "tranquil_query_runs", &json!({"limit": 5})).unwrap();
        assert_eq!(result.as_array().unwrap().len(), 1);
        assert_eq!(result[0]["run_id"], event["run_id"]);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn query_runs_filters_and_diff_tool_work() {
        let path = std::env::temp_dir().join(format!(
            "tranquil-rust-mcp-diff-{}.db",
            crate::util::now_millis()
        ));
        let storage = Storage::open(&path).unwrap();
        let run_a = normalize_event(
            "PostToolUse",
            &json!({
                "run_id": "mcp_run_a",
                "session_id": "mcp-a",
                "agent": "codex",
                "repo": "api",
                "branch": "main",
                "tool_name": "Bash",
                "tool_input": {"command": "cargo test"},
                "usage": {"cost_usd": 0.10},
                "labels": {"team": "alpha"}
            }),
            "hook",
        );
        let run_b = normalize_event(
            "PostToolUse",
            &json!({
                "run_id": "mcp_run_b",
                "session_id": "mcp-b",
                "agent": "codex",
                "repo": "api",
                "branch": "main",
                "tool_name": "Bash",
                "tool_input": {"command": "cargo build"},
                "usage": {"cost_usd": 0.30},
                "labels": {"team": "beta"}
            }),
            "hook",
        );
        storage.record_event(&run_a).unwrap();
        storage.record_event(&run_b).unwrap();
        let filtered = call_tool(
            &storage,
            "tranquil_query_runs",
            &json!({"limit": 5, "labels": "team=alpha"}),
        )
        .unwrap();
        assert_eq!(filtered.as_array().unwrap().len(), 1);
        assert_eq!(filtered[0]["run_id"], "mcp_run_a");
        let diff = call_tool(
            &storage,
            "tranquil_diff_runs",
            &json!({"a": "mcp_run_a", "b": "mcp_run_b"}),
        )
        .unwrap();
        assert_eq!(diff["delta"]["tool_calls"], 0);
        assert_eq!(diff["tools"]["b"][0], "Bash");
        let _ = std::fs::remove_file(path);
    }
}
