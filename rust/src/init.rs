use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};

use crate::config::Config;
use crate::util::{command_join, default_home};

const CLAUDE_EVENTS: &[(&str, bool)] = &[
    ("PostToolUse", true),
    ("PreToolUse", true),
    ("SessionStart", false),
    ("SessionEnd", false),
    ("UserPromptSubmit", false),
    ("SubagentStart", false),
    ("SubagentStop", false),
    ("PostToolUseFailure", true),
    ("PermissionRequest", false),
    ("PermissionDenied", false),
    ("TaskCreated", false),
    ("TaskCompleted", false),
    ("FileChanged", false),
    ("Stop", false),
    ("PreCompact", false),
];

const CODEX_EVENTS: &[(&str, bool)] = &[
    ("SessionStart", true),
    ("UserPromptSubmit", false),
    ("PreToolUse", true),
    ("PermissionRequest", true),
    ("PostToolUse", true),
    ("PreCompact", true),
    ("PostCompact", true),
    ("SubagentStart", true),
    ("SubagentStop", true),
    ("Stop", false),
];

#[derive(Debug, Default)]
pub struct InitReport {
    pub changed: Vec<String>,
    pub unchanged: Vec<String>,
    pub removed: Vec<String>,
    pub notes: Vec<String>,
}

impl InitReport {
    pub fn print(&self) {
        for item in &self.changed {
            println!("changed: {item}");
        }
        for item in &self.unchanged {
            println!("unchanged: {item}");
        }
        for item in &self.removed {
            println!("removed: {item}");
        }
        for item in &self.notes {
            println!("note: {item}");
        }
    }
}

pub fn run_init(
    config: &Config,
    agent: &str,
    scope: &str,
    undo: bool,
    cwd: &Path,
) -> Result<InitReport, String> {
    Config::save(config)?;
    let mut report = InitReport::default();
    report
        .unchanged
        .push(config.config_path().display().to_string());
    report
        .notes
        .push(format!("events write to {}", config.db_path.display()));
    if matches!(agent, "all" | "claude-code" | "claude") {
        let path = claude_settings_path(scope, cwd)?;
        if undo {
            undo_claude(&path, &mut report)?;
        } else {
            install_claude(&path, config, &mut report)?;
        }
    }
    if matches!(agent, "all" | "codex") {
        let path = codex_hooks_path(scope, cwd)?;
        if undo {
            undo_codex(&path, &mut report)?;
        } else {
            install_codex(&path, config, &mut report)?;
        }
    }
    report
        .notes
        .push("undo with: tranquil init --undo".to_string());
    Ok(report)
}

fn claude_settings_path(scope: &str, cwd: &Path) -> Result<PathBuf, String> {
    match scope {
        "user" => Ok(default_home()
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."))
            .join(".claude")
            .join("settings.json")),
        "project" => Ok(cwd.join(".claude").join("settings.json")),
        "local" => Ok(cwd.join(".claude").join("settings.local.json")),
        _ => Err("scope must be user, project, or local".to_string()),
    }
}

fn codex_hooks_path(scope: &str, cwd: &Path) -> Result<PathBuf, String> {
    match scope {
        "user" => Ok(default_home()
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."))
            .join(".codex")
            .join("hooks.json")),
        "project" | "local" => Ok(cwd.join(".codex").join("hooks.json")),
        _ => Err("scope must be user, project, or local".to_string()),
    }
}

fn install_claude(path: &Path, config: &Config, report: &mut InitReport) -> Result<(), String> {
    let original = read_json_object(path)?;
    let mut updated = original.clone();
    remove_tranquil_hooks(&mut updated);
    let hooks = object_mut(updated.as_object_mut().unwrap(), "hooks");
    for (event_name, needs_matcher) in CLAUDE_EVENTS {
        push_hook_entry(hooks, event_name, *needs_matcher, "claude-code", config);
    }
    updated["_tranquil"] = json!({
        "managed": true,
        "version": "0.1.0",
        "home": config.home,
        "events": CLAUDE_EVENTS.iter().map(|(name, _)| *name).collect::<Vec<_>>(),
        "runtime": "rust",
    });
    let mcp = object_mut(updated.as_object_mut().unwrap(), "mcpServers");
    mcp.insert(
        "tranquil".to_string(),
        json!({
            "command": "tranquil",
            "args": ["--home", config.home, "mcp"],
        }),
    );
    write_if_changed(path, &original, &updated, report, false)
}

fn install_codex(path: &Path, config: &Config, report: &mut InitReport) -> Result<(), String> {
    let original = read_json_object(path)?;
    let mut updated = original.clone();
    remove_tranquil_hooks(&mut updated);
    let hooks = object_mut(updated.as_object_mut().unwrap(), "hooks");
    for (event_name, needs_matcher) in CODEX_EVENTS {
        push_hook_entry(hooks, event_name, *needs_matcher, "codex", config);
    }
    updated["_tranquil"] = json!({
        "managed": true,
        "version": "0.1.0",
        "home": config.home,
        "events": CODEX_EVENTS.iter().map(|(name, _)| *name).collect::<Vec<_>>(),
        "runtime": "rust",
    });
    report
        .notes
        .push("codex hooks may require review with /hooks before Codex runs them".to_string());
    write_if_changed(path, &original, &updated, report, false)
}

fn undo_claude(path: &Path, report: &mut InitReport) -> Result<(), String> {
    if !path.exists() {
        report
            .unchanged
            .push(format!("{} (missing)", path.display()));
        return Ok(());
    }
    let original = read_json_object(path)?;
    let mut updated = original.clone();
    remove_tranquil_hooks(&mut updated);
    remove_tranquil_mcp(&mut updated);
    if let Some(map) = updated.as_object_mut() {
        map.remove("_tranquil");
    }
    write_if_changed(path, &original, &updated, report, true)
}

fn undo_codex(path: &Path, report: &mut InitReport) -> Result<(), String> {
    if !path.exists() {
        report
            .unchanged
            .push(format!("{} (missing)", path.display()));
        return Ok(());
    }
    let original = read_json_object(path)?;
    let mut updated = original.clone();
    remove_tranquil_hooks(&mut updated);
    if let Some(map) = updated.as_object_mut() {
        map.remove("_tranquil");
    }
    write_if_changed(path, &original, &updated, report, true)
}

fn push_hook_entry(
    hooks: &mut Map<String, Value>,
    event_name: &str,
    needs_matcher: bool,
    agent: &str,
    config: &Config,
) {
    let mut entry = json!({
        "hooks": [{
            "type": "command",
            "command": forward_command(config, event_name, agent),
            "timeout": 5,
            "statusMessage": "Sending event to Tranquil"
        }]
    });
    if needs_matcher {
        entry["matcher"] = Value::String("*".to_string());
    }
    hooks
        .entry(event_name.to_string())
        .or_insert_with(|| Value::Array(Vec::new()))
        .as_array_mut()
        .expect("hook event must be an array")
        .push(entry);
}

fn forward_command(config: &Config, event_name: &str, agent: &str) -> String {
    let exe = std::env::current_exe()
        .ok()
        .map(|path| path.display().to_string())
        .unwrap_or_else(|| "tranquil".to_string());
    command_join(&[
        exe,
        "--home".to_string(),
        config.home.display().to_string(),
        "hook-forwarder".to_string(),
        "--agent".to_string(),
        agent.to_string(),
        "--event".to_string(),
        event_name.to_string(),
    ])
}

fn remove_tranquil_hooks(value: &mut Value) {
    let Some(hooks) = value.get_mut("hooks").and_then(Value::as_object_mut) else {
        return;
    };
    let mut empty = Vec::new();
    for (event_name, entries) in hooks.iter_mut() {
        let Some(items) = entries.as_array_mut() else {
            continue;
        };
        items.retain(|entry| !entry_is_tranquil(entry));
        if items.is_empty() {
            empty.push(event_name.clone());
        }
    }
    for event_name in empty {
        hooks.remove(&event_name);
    }
    if hooks.is_empty() {
        value.as_object_mut().unwrap().remove("hooks");
    }
}

fn remove_tranquil_mcp(value: &mut Value) {
    let Some(servers) = value.get_mut("mcpServers").and_then(Value::as_object_mut) else {
        return;
    };
    let remove = servers
        .get("tranquil")
        .and_then(Value::as_object)
        .and_then(|server| server.get("command"))
        .and_then(Value::as_str)
        .is_some_and(|command| command == "tranquil" || command.contains("tranquil"));
    if remove {
        servers.remove("tranquil");
    }
    if servers.is_empty() {
        value.as_object_mut().unwrap().remove("mcpServers");
    }
}

fn entry_is_tranquil(entry: &Value) -> bool {
    entry
        .get("hooks")
        .and_then(Value::as_array)
        .is_some_and(|hooks| {
            hooks.iter().any(|hook| {
                let command = hook.get("command").and_then(Value::as_str).unwrap_or("");
                let url = hook.get("url").and_then(Value::as_str).unwrap_or("");
                url.contains("/hooks/")
                    || (command.contains("tranquil")
                        && (command.contains("hook-forwarder")
                            || command.contains("hook_forwarder")
                            || command.contains("hook_forwarder")))
            })
        })
}

fn object_mut<'a>(map: &'a mut Map<String, Value>, key: &str) -> &'a mut Map<String, Value> {
    map.entry(key.to_string())
        .or_insert_with(|| Value::Object(Map::new()))
        .as_object_mut()
        .expect("managed JSON key must be an object")
}

fn read_json_object(path: &Path) -> Result<Value, String> {
    if !path.exists() {
        return Ok(Value::Object(Map::new()));
    }
    let text = std::fs::read_to_string(path).map_err(|err| err.to_string())?;
    let value: Value = serde_json::from_str(&text)
        .map_err(|err| format!("{} is not valid JSON: {err}", path.display()))?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(format!("{} must contain a JSON object", path.display()))
    }
}

fn write_if_changed(
    path: &Path,
    original: &Value,
    updated: &Value,
    report: &mut InitReport,
    removed: bool,
) -> Result<(), String> {
    if original == updated {
        report.unchanged.push(path.display().to_string());
        return Ok(());
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|err| err.to_string())?;
    }
    std::fs::write(
        path,
        serde_json::to_string_pretty(updated).map_err(|err| err.to_string())? + "\n",
    )
    .map_err(|err| err.to_string())?;
    if removed {
        report.removed.push(path.display().to_string());
    } else {
        report.changed.push(path.display().to_string());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn project_codex_init_is_idempotent_and_reversible() {
        let root =
            std::env::temp_dir().join(format!("tranquil-rust-init-{}", crate::util::now_millis()));
        let home = root.join("home");
        let project = root.join("project");
        std::fs::create_dir_all(&project).unwrap();
        let config = Config::load(Some(home), true).unwrap();
        let first = run_init(&config, "codex", "project", false, &project).unwrap();
        assert!(!first.changed.is_empty());
        let hooks_path = project.join(".codex").join("hooks.json");
        let data: Value =
            serde_json::from_str(&std::fs::read_to_string(&hooks_path).unwrap()).unwrap();
        assert_eq!(data["_tranquil"]["runtime"], "rust");
        assert!(
            data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
                .as_str()
                .unwrap()
                .contains("hook-forwarder")
        );
        let second = run_init(&config, "codex", "project", false, &project).unwrap();
        assert!(
            second
                .unchanged
                .iter()
                .any(|item| item == &hooks_path.display().to_string())
        );
        let undo = run_init(&config, "codex", "project", true, &project).unwrap();
        assert!(
            undo.removed
                .iter()
                .any(|item| item == &hooks_path.display().to_string())
        );
        let data: Value =
            serde_json::from_str(&std::fs::read_to_string(&hooks_path).unwrap()).unwrap();
        assert!(data.get("hooks").is_none());
        let _ = std::fs::remove_dir_all(root);
    }
}
