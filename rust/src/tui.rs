use std::io::{self, IsTerminal, Write};
use std::time::Duration;

use crossterm::{
    cursor,
    event::{self, Event, KeyCode, KeyEvent, KeyModifiers},
    execute,
    terminal::{self, ClearType},
};
use serde_json::Value;

use crate::config::Config;
use crate::storage::{RunFilters, RunSummary, Storage, extract_tool_command};
use crate::tailer;

pub struct TuiOptions {
    pub once: bool,
    pub run_id: Option<String>,
    pub filters: RunFilters,
    pub events_limit: usize,
    pub interval: f64,
}

pub fn run_tui(storage: &Storage, config: &Config, options: TuiOptions) -> Result<(), String> {
    let mut state = TuiState {
        current_run_id: options.run_id.clone(),
        selected_index: 0,
        message: None,
        tail_state: tailer::TailState::default(),
    };
    if options.once {
        let output = render_current(storage, config, &options, &mut state)?;
        println!("{output}");
        return Ok(());
    }
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        loop {
            let output = render_current(storage, config, &options, &mut state)?;
            print!("\x1b[2J\x1b[H{output}\n");
            io::stdout().flush().map_err(|err| err.to_string())?;
            std::thread::sleep(Duration::from_secs_f64(options.interval.max(0.5)));
        }
    }
    let _guard = TerminalGuard::enter()?;
    loop {
        let output = render_current(storage, config, &options, &mut state)?;
        let runs = storage.list_runs_filtered(&options.filters)?;
        let mut stdout = io::stdout();
        execute!(
            stdout,
            terminal::Clear(ClearType::All),
            cursor::MoveTo(0, 0)
        )
        .map_err(|err| err.to_string())?;
        stdout
            .write_all(output.as_bytes())
            .map_err(|err| err.to_string())?;
        stdout.write_all(b"\n").map_err(|err| err.to_string())?;
        stdout.flush().map_err(|err| err.to_string())?;
        if !event::poll(Duration::from_secs_f64(options.interval.max(0.05)))
            .map_err(|err| err.to_string())?
        {
            continue;
        }
        let Event::Key(key) = event::read().map_err(|err| err.to_string())? else {
            continue;
        };
        if handle_key(storage, &mut state, &runs, key)? {
            return Ok(());
        }
    }
}

#[allow(dead_code)]
pub fn render_fleet(
    storage: &Storage,
    filters: &RunFilters,
    ingested: usize,
) -> Result<String, String> {
    render_fleet_with_state(storage, filters, ingested, 0, None)
}

fn render_fleet_with_state(
    storage: &Storage,
    filters: &RunFilters,
    ingested: usize,
    selected_index: usize,
    message: Option<&str>,
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
        let selected_index = clamp_index(selected_index, runs.len());
        for (index, run) in runs.iter().enumerate() {
            lines.push(run_line(run, index == selected_index));
        }
        if let Some(run) = runs.get(selected_index) {
            lines.push(String::new());
            lines.push("Peek".to_string());
            lines.push(format!(
                "{} {} tools={} files={} signals={} subagents={}",
                run.run_id,
                run.status,
                run.tool_calls,
                run.files_touched,
                run.signals_count,
                run.subagents_count
            ));
            if let Some(prompt) = &run.first_prompt {
                lines.push(format!("prompt: {}", truncate(prompt, 140)));
            }
            if let Some(message) = &run.latest_message {
                lines.push(format!("latest: {}", truncate(message, 140)));
            }
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
    lines.push(String::new());
    lines.push(
        message
            .map(|message| format!("message: {message}"))
            .unwrap_or_else(|| {
                "keys: j/n/down next | p/up prev | enter/o/right open | b/left fleet | k stop | r refresh | q quit"
                    .to_string()
            }),
    );
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
    lines.push(String::new());
    lines.push("keys: b/left fleet | k stop | r refresh | q quit".to_string());
    Ok(lines.join("\n"))
}

fn run_line(run: &RunSummary, selected: bool) -> String {
    let repo = run.repo.as_deref().unwrap_or("unknown");
    let branch = run.branch.as_deref().unwrap_or("-");
    format!(
        "{} {:10} {:12} ${:>9.2}  {:30} {}",
        if selected { ">" } else { " " },
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

struct TuiState {
    current_run_id: Option<String>,
    selected_index: usize,
    message: Option<String>,
    tail_state: tailer::TailState,
}

struct TerminalGuard;

impl TerminalGuard {
    fn enter() -> Result<Self, String> {
        terminal::enable_raw_mode().map_err(|err| err.to_string())?;
        execute!(io::stdout(), terminal::EnterAlternateScreen, cursor::Hide)
            .map_err(|err| err.to_string())?;
        Ok(Self)
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = execute!(io::stdout(), cursor::Show, terminal::LeaveAlternateScreen);
        let _ = terminal::disable_raw_mode();
    }
}

fn render_current(
    storage: &Storage,
    config: &Config,
    options: &TuiOptions,
    state: &mut TuiState,
) -> Result<String, String> {
    let ingested = tailer::scan_configured_once(storage, config, &mut state.tail_state)?;
    if let Some(run_id) = &state.current_run_id {
        let mut output = render_run(storage, run_id, options.events_limit)?;
        if let Some(message) = &state.message {
            output.push('\n');
            output.push_str("message: ");
            output.push_str(message);
        }
        return Ok(output);
    }
    render_fleet_with_state(
        storage,
        &options.filters,
        ingested,
        state.selected_index,
        state.message.as_deref(),
    )
}

fn handle_key(
    storage: &Storage,
    state: &mut TuiState,
    runs: &[RunSummary],
    key: KeyEvent,
) -> Result<bool, String> {
    state.message = None;
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
        return Ok(true);
    }
    match key.code {
        KeyCode::Char('q') | KeyCode::Esc => return Ok(true),
        KeyCode::Char('r') => return Ok(false),
        KeyCode::Char('b') | KeyCode::Left => {
            state.current_run_id = None;
            return Ok(false);
        }
        KeyCode::Char('k') => {
            let target = state.current_run_id.clone().or_else(|| {
                runs.get(clamp_index(state.selected_index, runs.len()))
                    .map(|run| run.run_id.clone())
            });
            if let Some(run_id) = target {
                storage.request_stop(&run_id, "tui_stop")?;
                state.message = Some(format!("stop requested for {run_id}"));
            } else {
                state.message = Some("no run selected".to_string());
            }
            return Ok(false);
        }
        _ => {}
    }
    if state.current_run_id.is_some() {
        return Ok(false);
    }
    match key.code {
        KeyCode::Char('j') | KeyCode::Char('n') | KeyCode::Down => {
            state.selected_index = clamp_index(state.selected_index.saturating_add(1), runs.len());
        }
        KeyCode::Char('p') | KeyCode::Up => {
            state.selected_index = state.selected_index.saturating_sub(1);
        }
        KeyCode::Enter | KeyCode::Char('o') | KeyCode::Right => {
            if let Some(run) = runs.get(clamp_index(state.selected_index, runs.len())) {
                state.current_run_id = Some(run.run_id.clone());
            }
        }
        _ => {}
    }
    Ok(false)
}

fn clamp_index(index: usize, len: usize) -> usize {
    if len == 0 { 0 } else { index.min(len - 1) }
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

    #[test]
    fn keyboard_handler_navigates_opens_and_stops_selected_run() {
        let root = std::env::temp_dir().join(format!(
            "tranquil-rust-tui-keys-{}",
            crate::util::now_millis()
        ));
        let storage = Storage::open(&root.join("tranquil.db")).unwrap();
        for (run_id, ts) in [("tui_keys_a", "1000"), ("tui_keys_b", "2000")] {
            let event = normalize_event(
                "UserPromptSubmit",
                &json!({"run_id": run_id, "session_id": run_id, "ts": ts, "prompt": run_id}),
                "hook",
            );
            storage.record_event(&event).unwrap();
        }
        let filters = RunFilters {
            limit: 10,
            ..RunFilters::default()
        };
        let runs = storage.list_runs_filtered(&filters).unwrap();
        assert_eq!(runs.len(), 2);
        let mut state = TuiState {
            current_run_id: None,
            selected_index: 0,
            message: None,
            tail_state: crate::tailer::TailState::default(),
        };
        handle_key(
            &storage,
            &mut state,
            &runs,
            KeyEvent::new(KeyCode::Down, KeyModifiers::NONE),
        )
        .unwrap();
        assert_eq!(state.selected_index, 1);
        handle_key(
            &storage,
            &mut state,
            &runs,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        )
        .unwrap();
        assert_eq!(
            state.current_run_id.as_deref(),
            Some(runs[1].run_id.as_str())
        );
        handle_key(
            &storage,
            &mut state,
            &runs,
            KeyEvent::new(KeyCode::Left, KeyModifiers::NONE),
        )
        .unwrap();
        assert!(state.current_run_id.is_none());
        handle_key(
            &storage,
            &mut state,
            &runs,
            KeyEvent::new(KeyCode::Char('k'), KeyModifiers::NONE),
        )
        .unwrap();
        assert!(
            storage
                .has_active_signal(&runs[1].run_id, "stop_requested")
                .unwrap()
        );
        let _ = std::fs::remove_dir_all(root);
    }
}
