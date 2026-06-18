use serde_json::{Value, json};

use crate::config::Config;
use crate::storage::{Storage, extract_paths, extract_tool_command};

pub fn pre_tool_decision(
    storage: &Storage,
    config: &Config,
    event: &Value,
) -> Result<Option<String>, String> {
    let run_id = event
        .get("run_id")
        .and_then(Value::as_str)
        .ok_or_else(|| "missing run_id".to_string())?;
    if let Some(policy) = policy_violation(config, event) {
        if storage.get_run(run_id)?.is_some() {
            storage.add_signal(
                run_id,
                "policy_denied",
                "high",
                json!({
                    "reason": policy.reason,
                    "path": policy.path,
                    "command": policy.command,
                    "pattern": policy.pattern,
                    "message": policy.message,
                    "fingerprint": policy.fingerprint,
                }),
                Some("deny_pre_tool"),
            )?;
        }
        return Ok(Some(format!("Tranquil policy: {}", policy.message)));
    }
    if storage.has_active_signal(run_id, "stop_requested")? {
        return Ok(Some(
            "Tranquil stop requested: future tool calls for this run are denied.".to_string(),
        ));
    }
    if !config.kill_switch_enabled {
        return Ok(None);
    }
    let Some(run) = storage.get_run(run_id)? else {
        return Ok(None);
    };
    if run.total_cost_usd_est >= config.run_cost_budget_usd {
        storage.add_signal(
            run_id,
            "runaway_cost",
            "high",
            json!({
                "reason": "pre_tool_kill_switch_budget",
                "cost_usd_est": run.total_cost_usd_est,
                "budget_usd": config.run_cost_budget_usd,
                "fingerprint": "kill_switch",
            }),
            Some("deny_pre_tool"),
        )?;
        return Ok(Some(format!(
            "Tranquil kill switch: run cost ${:.2} est. is over budget ${:.2}.",
            run.total_cost_usd_est, config.run_cost_budget_usd
        )));
    }
    Ok(None)
}

fn policy_violation(config: &Config, event: &Value) -> Option<PolicyViolation> {
    if !config.policy_enabled {
        return None;
    }
    for path in extract_paths(event) {
        for pattern in &config.policy_forbidden_paths {
            if wildcard_match(pattern, &path) {
                return Some(PolicyViolation {
                    reason: "forbidden_path".to_string(),
                    path: Some(path.clone()),
                    command: None,
                    pattern: pattern.clone(),
                    message: format!("path {path} matches forbidden pattern {pattern}"),
                    fingerprint: format!("path:{pattern}"),
                });
            }
        }
    }
    if let Some(command) = extract_tool_command(event) {
        for pattern in &config.policy_forbidden_commands {
            if command.contains(pattern) || wildcard_match(pattern, &command) {
                return Some(PolicyViolation {
                    reason: "forbidden_command".to_string(),
                    path: None,
                    command: Some(command.clone()),
                    pattern: pattern.clone(),
                    message: format!("command matches forbidden pattern {pattern}"),
                    fingerprint: format!("command:{pattern}"),
                });
            }
        }
    }
    None
}

struct PolicyViolation {
    reason: String,
    path: Option<String>,
    command: Option<String>,
    pattern: String,
    message: String,
    fingerprint: String,
}

fn wildcard_match(pattern: &str, value: &str) -> bool {
    if pattern == "*" {
        return true;
    }
    if !pattern.contains('*') {
        return pattern == value;
    }
    let mut remaining = value;
    let starts_with_wildcard = pattern.starts_with('*');
    let ends_with_wildcard = pattern.ends_with('*');
    let parts: Vec<&str> = pattern.split('*').filter(|part| !part.is_empty()).collect();
    if parts.is_empty() {
        return true;
    }
    if !starts_with_wildcard {
        let first = parts[0];
        if !remaining.starts_with(first) {
            return false;
        }
        remaining = &remaining[first.len()..];
    }
    for (index, part) in parts.iter().enumerate() {
        if index == 0 && !starts_with_wildcard {
            continue;
        }
        let Some(offset) = remaining.find(part) else {
            return false;
        };
        remaining = &remaining[offset + part.len()..];
    }
    if !ends_with_wildcard {
        if let Some(last) = parts.last() {
            return value.ends_with(last);
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{Config, SignalThresholds};
    use crate::normalize::normalize_event;
    use crate::storage::Storage;

    #[test]
    fn wildcard_matches_basic_patterns() {
        assert!(wildcard_match("secrets/**", "secrets/api.env"));
        assert!(wildcard_match("*.env", ".env"));
        assert!(!wildcard_match("src/*.rs", "src/main.py"));
    }

    #[test]
    fn denies_forbidden_path() {
        let root = std::env::temp_dir().join(format!(
            "tranquil-rust-policy-{}",
            crate::util::now_millis()
        ));
        let config = Config {
            home: root.clone(),
            db_path: root.join("tranquil.db"),
            token: "tok".to_string(),
            raw_payloads: true,
            signal_thresholds: SignalThresholds::default(),
            kill_switch_enabled: false,
            run_cost_budget_usd: 10.0,
            policy_enabled: true,
            policy_forbidden_paths: vec![".env".to_string(), "secrets/**".to_string()],
            policy_forbidden_commands: Vec::new(),
            codex_rollout_paths: Vec::new(),
            transcript_paths: Vec::new(),
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
        let seed = normalize_event(
            "UserPromptSubmit",
            &json!({"session_id": "policy", "prompt": "edit config"}),
            "hook",
        );
        storage.record_event(&seed).unwrap();
        let event = normalize_event(
            "PreToolUse",
            &json!({
                "session_id": "policy",
                "tool_name": "Write",
                "tool_input": {"file_path": ".env"}
            }),
            "hook",
        );
        let decision = pre_tool_decision(&storage, &config, &event).unwrap();
        assert!(decision.unwrap().contains(".env"));
        assert!(
            storage
                .list_run_signals(event["run_id"].as_str().unwrap())
                .unwrap()
                .iter()
                .any(|signal| signal.signal_type == "policy_denied")
        );
        let _ = std::fs::remove_dir_all(root);
    }
}
