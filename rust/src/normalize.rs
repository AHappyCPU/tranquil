use serde_json::{Value, json};

use crate::util::{as_str, git_context, new_id, parse_ts, redact, safe_f64, safe_i64, stable_id};

pub fn normalize_event(event_hint: &str, payload: &Value, source: &str) -> Value {
    let event_type = normalize_event_type(
        as_str(
            payload,
            &["event_type", "hook_event_name", "hookEventName", "type"],
        )
        .unwrap_or(event_hint),
    );
    let agent = normalize_agent(as_str(payload, &["agent", "vendor", "source_agent", "app"]));
    let ts = parse_ts(
        payload
            .get("ts")
            .or_else(|| payload.get("timestamp"))
            .or_else(|| payload.get("created_at"))
            .or_else(|| payload.get("time")),
    );
    let cwd = extract_cwd(payload).map(str::to_string);
    let (repo, branch) = extract_repo_branch(payload, cwd.as_deref());
    let session_id = as_str(
        payload,
        &["session_id", "sessionId", "conversation_id", "rollout_id"],
    )
    .map(str::to_string)
    .unwrap_or_else(|| {
        stable_id(
            "session",
            &[agent.clone(), ts.clone(), cwd.clone().unwrap_or_default()],
        )
    });
    let parent_session_id = as_str(
        payload,
        &["parent_session_id", "parentSessionId", "parent_id"],
    )
    .map(str::to_string);
    let run_seed = as_str(payload, &["run_id", "runId"])
        .map(str::to_string)
        .or_else(|| parent_session_id.clone())
        .unwrap_or_else(|| session_id.clone());
    let run_id = as_str(payload, &["run_id", "runId"])
        .map(str::to_string)
        .unwrap_or_else(|| stable_id("run", &[agent.clone(), run_seed]));
    let tool = extract_tool(payload);
    let usage = extract_usage(payload);
    let permission = extract_permission(payload);
    let message = extract_message(payload);
    let labels = payload
        .get("labels")
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}));
    json!({
        "schema": "tranquil.event/v1",
        "event_id": as_str(payload, &["event_id", "id"]).map(str::to_string).unwrap_or_else(|| new_id("evt")),
        "ts": ts,
        "source": source,
        "agent": agent,
        "agent_version": as_str(payload, &["agent_version", "version"]),
        "event_type": event_type,
        "run_id": run_id,
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "depth": safe_i64(payload.get("depth").or_else(|| payload.get("subagent_depth"))).unwrap_or(0),
        "context": {
            "cwd": cwd,
            "repo": repo,
            "branch": branch,
            "worktree": as_str(payload, &["worktree", "worktree_path"]),
            "permission_mode": as_str(payload, &["permission_mode", "permissionMode"]),
        },
        "model": as_str(payload, &["model", "requested_model", "requestedModel"]),
        "resolved_model": as_str(payload, &["resolved_model", "resolvedModel", "actual_model", "model"]),
        "tool": tool,
        "usage": usage,
        "permission": permission,
        "labels": labels,
        "message": message,
        "raw": redact(payload),
    })
}

fn normalize_event_type(value: &str) -> String {
    let folded = value.trim().replace('_', "-").to_ascii_lowercase();
    match folded.as_str() {
        "sessionstart" | "session-start" => "session_start",
        "sessionend" | "session-end" => "session_end",
        "userpromptsubmit" | "user-prompt-submit" | "user" | "user-message" => "user_prompt",
        "assistant" | "assistant-message" => "stop",
        "pretooluse" | "pre-tool-use" => "pre_tool",
        "posttooluse" | "post-tool-use" => "post_tool",
        "posttoolusefailure" | "post-tool-use-failure" | "tool-failure" => "tool_failure",
        "permissionrequest" | "permission-request" => "permission_request",
        "permissiondenied" | "permission-denied" => "permission_denied",
        "subagentstart" | "subagent-start" => "subagent_start",
        "subagentstop" | "subagent-stop" => "subagent_stop",
        "taskcreated" | "task-created" => "task_created",
        "taskcompleted" | "task-completed" => "task_completed",
        "filechanged" | "file-changed" => "file_changed",
        "precompact" | "pre-compact" | "postcompact" | "post-compact" => "compact",
        other => return other.replace('-', "_"),
    }
    .to_string()
}

fn normalize_agent(value: Option<&str>) -> String {
    let text = value.unwrap_or("").to_ascii_lowercase();
    if text.contains("codex") || text == "cx" || text == "openai" {
        "codex".to_string()
    } else {
        "claude-code".to_string()
    }
}

fn extract_cwd(payload: &Value) -> Option<&str> {
    as_str(payload, &["cwd", "working_directory", "workdir"])
        .or_else(|| payload.get("context").and_then(|ctx| as_str(ctx, &["cwd"])))
}

fn extract_repo_branch(payload: &Value, cwd: Option<&str>) -> (Option<String>, Option<String>) {
    let repo = as_str(payload, &["repo"])
        .or_else(|| {
            payload
                .get("context")
                .and_then(|ctx| as_str(ctx, &["repo"]))
        })
        .map(str::to_string);
    let branch = as_str(payload, &["branch"])
        .or_else(|| {
            payload
                .get("context")
                .and_then(|ctx| as_str(ctx, &["branch"]))
        })
        .map(str::to_string);
    if repo.is_some() && branch.is_some() {
        return (repo, branch);
    }
    let (git_repo, git_branch) = git_context(cwd);
    (repo.or(git_repo), branch.or(git_branch))
}

fn extract_tool(payload: &Value) -> Value {
    let raw_tool = payload
        .get("tool")
        .filter(|value| value.is_object())
        .unwrap_or(&Value::Null);
    let name = as_str(payload, &["tool_name", "toolName", "name"])
        .or_else(|| as_str(raw_tool, &["name"]))
        .unwrap_or("unknown");
    let input = payload
        .get("tool_input")
        .or_else(|| payload.get("toolInput"))
        .or_else(|| raw_tool.get("input"))
        .or_else(|| payload.get("input"))
        .cloned();
    let output = payload
        .get("tool_output")
        .or_else(|| payload.get("toolOutput"))
        .or_else(|| raw_tool.get("output"))
        .or_else(|| payload.get("output"))
        .or_else(|| payload.get("result"))
        .cloned();
    let duration_ms = safe_i64(
        payload
            .get("duration_ms")
            .or_else(|| payload.get("durationMs"))
            .or_else(|| raw_tool.get("duration_ms"))
            .or_else(|| raw_tool.get("durationMs")),
    );
    if name == "unknown" && input.is_none() && output.is_none() && duration_ms.is_none() {
        return Value::Null;
    }
    let mut out = output.unwrap_or(Value::Null);
    if let Some(exit_code) = safe_i64(
        payload
            .get("exit_code")
            .or_else(|| raw_tool.get("exit_code")),
    ) {
        out = match out {
            Value::Object(mut map) => {
                map.entry("exit_code".to_string())
                    .or_insert(json!(exit_code));
                Value::Object(map)
            }
            other => json!({ "summary": other, "exit_code": exit_code }),
        };
    }
    json!({
        "name": name,
        "input": input.unwrap_or(Value::Null),
        "output": out,
        "duration_ms": duration_ms,
    })
}

fn extract_usage(payload: &Value) -> Value {
    let usage = payload
        .get("usage")
        .filter(|value| value.is_object())
        .unwrap_or(&Value::Null);
    json!({
        "input_tokens": safe_i64(usage.get("input_tokens").or_else(|| usage.get("inputTokens")).or_else(|| payload.get("input_tokens"))),
        "output_tokens": safe_i64(usage.get("output_tokens").or_else(|| usage.get("outputTokens")).or_else(|| payload.get("output_tokens"))),
        "cache_read_tokens": safe_i64(usage.get("cache_read_tokens").or_else(|| usage.get("cacheReadTokens"))),
        "cache_write_tokens": safe_i64(usage.get("cache_write_tokens").or_else(|| usage.get("cacheWriteTokens"))),
        "cost_usd_est": safe_f64(
            usage.get("cost_usd_est")
                .or_else(|| usage.get("total_cost_usd"))
                .or_else(|| usage.get("cost_usd"))
                .or_else(|| payload.get("cost_usd_est"))
                .or_else(|| payload.get("total_cost_usd"))
        ),
    })
}

fn extract_permission(payload: &Value) -> Value {
    let raw = payload
        .get("permission")
        .filter(|value| value.is_object())
        .unwrap_or(&Value::Null);
    let decision = as_str(payload, &["permission_decision", "permissionDecision"])
        .or_else(|| as_str(raw, &["decision"]));
    let reason = as_str(payload, &["permission_reason", "permissionReason"])
        .or_else(|| as_str(raw, &["reason"]));
    if decision.is_none() && reason.is_none() {
        Value::Null
    } else {
        json!({ "decision": decision, "reason": reason })
    }
}

fn extract_message(payload: &Value) -> Value {
    if let Some(text) = as_str(
        payload,
        &[
            "message",
            "assistant_message",
            "assistantMessage",
            "final_text",
            "text",
            "content",
            "prompt",
        ],
    ) {
        return Value::String(text.trim().to_string());
    }
    if let Some(content) = payload.get("message").and_then(|msg| msg.get("content")) {
        if let Some(text) = content.as_str() {
            return Value::String(text.trim().to_string());
        }
        if let Some(items) = content.as_array() {
            let parts: Vec<String> = items
                .iter()
                .filter_map(|item| item.get("text").and_then(Value::as_str).map(str::to_string))
                .collect();
            if !parts.is_empty() {
                return Value::String(parts.join("\n"));
            }
        }
    }
    Value::Null
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_codex_tool_event() {
        let event = normalize_event(
            "PostToolUse",
            &json!({
                "agent": "codex",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest"},
                "usage": {"cost_usd": 0.25}
            }),
            "hook",
        );
        assert_eq!(event["event_type"], "post_tool");
        assert_eq!(event["agent"], "codex");
        assert_eq!(event["tool"]["name"], "Bash");
        assert_eq!(event["usage"]["cost_usd_est"], 0.25);
    }
}
