use serde_json::Value;

use crate::storage::{RunFilters, RunSummary, Storage, extract_tool_command};

pub fn render_fleet(
    storage: &Storage,
    filters: &RunFilters,
    ingested: usize,
) -> Result<String, String> {
    let stats = storage.stats()?;
    let mut lines = Vec::new();
    lines.push(format!(
        "Tranquil {} live / {} runs | ${:.2} est. | {} signaled | +{} tailed",
        stats.live, stats.runs, stats.cost_usd_est, stats.signaled, ingested
    ));
    lines.push(format!(
        "{:10} {:12} {:>10}  {:30} RUN",
        "STATE", "AGENT", "COST EST.", "REPO / BRANCH"
    ));
    let runs = storage.list_runs_filtered(filters)?;
    if runs.is_empty() {
        lines.push("No runs captured yet.".to_string());
    } else {
        for run in runs {
            lines.push(run_line(&run));
        }
    }
    let signals = storage.list_signals(Some(true), 5)?;
    if !signals.is_empty() {
        lines.push(String::new());
        lines.push("Active signals".to_string());
        for signal in signals {
            lines.push(format!(
                "{:6} {:18} {}",
                signal.severity, signal.signal_type, signal.run_id
            ));
        }
    }
    let eval_runs = storage.list_eval_runs(None, 5)?;
    if !eval_runs.is_empty() {
        lines.push(String::new());
        lines.push("Recent evals".to_string());
        for eval_run in eval_runs {
            lines.push(format!(
                "{:18} {:10} passed={} failed={}",
                truncate(&eval_run.suite, 18),
                truncate(&eval_run.status, 10),
                eval_run.passed_count,
                eval_run.failed_count
            ));
        }
    }
    Ok(lines.join("\n"))
}

pub fn render_run(storage: &Storage, run_id: &str, events_limit: usize) -> Result<String, String> {
    let run = storage
        .get_run(run_id)?
        .ok_or_else(|| format!("run not found: {run_id}"))?;
    let mut lines = Vec::new();
    lines.push(format!("Run {}", run.run_id));
    lines.push(format!(
        "{} {} ${:.2} repo={} branch={}",
        run.status,
        run.agent,
        run.total_cost_usd_est,
        run.repo.as_deref().unwrap_or("unknown"),
        run.branch.as_deref().unwrap_or("-")
    ));
    if !run.labels.is_empty() {
        let labels = run
            .labels
            .iter()
            .map(|(key, values)| {
                if values.is_empty() {
                    key.to_string()
                } else {
                    format!("{key}={}", values.join("|"))
                }
            })
            .collect::<Vec<_>>()
            .join(", ");
        lines.push(format!("labels: {}", truncate(&labels, 140)));
    }
    if run.subagents_count > 0 {
        lines.push(format!(
            "subagents: {} max_depth={}",
            run.subagents_count, run.max_depth
        ));
    }
    if let Some(prompt) = &run.first_prompt {
        lines.push(format!("prompt: {}", truncate(prompt, 120)));
    }
    let scores = storage.list_run_scores(run_id, 20)?;
    if !scores.is_empty() {
        lines.push(String::new());
        lines.push("Scores".to_string());
        for score in scores {
            lines.push(format!(
                "{} {:20} {:18} value={}",
                if score.passed { "PASS" } else { "FAIL" },
                truncate(&score.suite, 20),
                truncate(&score.scorer, 18),
                score
                    .value
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "null".to_string())
            ));
        }
    }
    let signals = storage.list_run_signals(run_id)?;
    if !signals.is_empty() {
        lines.push(String::new());
        lines.push("Signals".to_string());
        for signal in signals {
            lines.push(format!(
                "{:6} {:18} {}",
                signal.severity,
                signal.signal_type,
                serde_json::to_string(&signal.evidence).map_err(|err| err.to_string())?
            ));
        }
    }
    let subagents = storage.list_subagents(run_id)?;
    if !subagents.is_empty() {
        lines.push(String::new());
        lines.push("Subagents".to_string());
        for subagent in subagents {
            lines.push(format!(
                "{:10} depth={} tools={} ${:.2} {}",
                truncate(&subagent.status, 10),
                subagent.depth,
                subagent.tool_calls,
                subagent.cost_usd_est,
                truncate(&subagent.session_id, 50)
            ));
        }
    }
    let files = storage.file_touch_summary(run_id)?;
    if !files.is_empty() {
        lines.push(String::new());
        lines.push("Files".to_string());
        for file in files.iter().take(10) {
            lines.push(format!(
                "{} reads={} writes={} events={} tools={}",
                truncate(&file.path, 64),
                file.reads,
                file.writes,
                file.events,
                truncate(&file.tools.join(","), 40)
            ));
        }
    }
    let events = storage.get_run_display_events(run_id)?;
    lines.push(String::new());
    lines.push("Events".to_string());
    let start = events.len().saturating_sub(events_limit);
    for event in events.iter().skip(start) {
        lines.push(event_line(event));
    }
    Ok(lines.join("\n"))
}

fn run_line(run: &RunSummary) -> String {
    let repo = run.repo.as_deref().unwrap_or("unknown");
    let branch = run.branch.as_deref().unwrap_or("-");
    format!(
        "{:10} {:12} ${:>9.2}  {:30} {}",
        truncate(&run.status, 10),
        truncate(&run.agent, 12),
        run.total_cost_usd_est,
        truncate(&format!("{repo} / {branch}"), 30),
        run.run_id
    )
}

fn event_line(event: &Value) -> String {
    let event_type = event
        .get("event_type")
        .and_then(Value::as_str)
        .unwrap_or("event");
    let ts = event.get("ts").and_then(Value::as_str).unwrap_or("");
    let detail = extract_tool_command(event)
        .or_else(|| {
            event
                .get("tool")
                .and_then(|tool| tool.get("name"))
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .or_else(|| {
            event
                .get("message")
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .unwrap_or_default();
    let mut line = format!(
        "{:14} {:18} {}",
        truncate(event_type, 14),
        truncate(ts, 18),
        truncate(&detail, 100)
    );
    if let Some(diff) = event.get("diff") {
        let kind = diff.get("kind").and_then(Value::as_str).unwrap_or("diff");
        let path = diff.get("path").and_then(Value::as_str).unwrap_or("");
        line.push_str(&format!(" [{} {}]", kind, truncate(path, 50)));
    }
    line
}

fn truncate(value: &str, max: usize) -> String {
    if value.chars().count() <= max {
        value.to_string()
    } else {
        value
            .chars()
            .take(max.saturating_sub(1))
            .collect::<String>()
            + "."
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::normalize::normalize_event;
    use serde_json::json;

    #[test]
    fn renders_fleet_and_run() {
        let root =
            std::env::temp_dir().join(format!("tranquil-rust-tui-{}", crate::util::now_millis()));
        let storage = Storage::open(&root.join("tranquil.db")).unwrap();
        let event = normalize_event(
            "UserPromptSubmit",
            &json!({"session_id": "tui", "prompt": "show status"}),
            "hook",
        );
        let run_id = event["run_id"].as_str().unwrap().to_string();
        storage.record_event(&event).unwrap();
        assert!(
            render_fleet(
                &storage,
                &RunFilters {
                    limit: 10,
                    ..RunFilters::default()
                },
                0
            )
            .unwrap()
            .contains("Tranquil")
        );
        assert!(
            render_run(&storage, &run_id, 10)
                .unwrap()
                .contains("show status")
        );
        let _ = std::fs::remove_dir_all(root);
    }
}
