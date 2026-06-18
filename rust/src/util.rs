use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;

static ID_COUNTER: AtomicU64 = AtomicU64::new(0);

pub fn default_home() -> PathBuf {
    if let Ok(home) = std::env::var("TRANQUIL_HOME") {
        return expand_home(&home);
    }
    let base = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_else(|_| ".".to_string());
    PathBuf::from(base).join(".tranquil")
}

pub fn expand_home(value: &str) -> PathBuf {
    if value == "~" {
        return PathBuf::from(home_dir());
    }
    if let Some(rest) = value
        .strip_prefix("~/")
        .or_else(|| value.strip_prefix("~\\"))
    {
        return PathBuf::from(home_dir()).join(rest);
    }
    PathBuf::from(value)
}

fn home_dir() -> String {
    std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_else(|_| ".".to_string())
}

pub fn now_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

pub fn now_ts() -> String {
    format!("{}", now_millis())
}

pub fn parse_ts(value: Option<&Value>) -> String {
    match value {
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::String(text)) if !text.trim().is_empty() => text.clone(),
        _ => now_ts(),
    }
}

pub fn stable_id(prefix: &str, parts: &[String]) -> String {
    let mut hasher = DefaultHasher::new();
    for part in parts {
        part.hash(&mut hasher);
        "\x1f".hash(&mut hasher);
    }
    format!("{prefix}_{:016x}", hasher.finish())
}

pub fn new_id(prefix: &str) -> String {
    stable_id(
        prefix,
        &[
            now_millis().to_string(),
            std::process::id().to_string(),
            ID_COUNTER.fetch_add(1, Ordering::Relaxed).to_string(),
        ],
    )
}

pub fn as_str<'a>(value: &'a Value, keys: &[&str]) -> Option<&'a str> {
    for key in keys {
        if let Some(text) = value.get(*key).and_then(Value::as_str) {
            if !text.is_empty() {
                return Some(text);
            }
        }
    }
    None
}

pub fn safe_i64(value: Option<&Value>) -> Option<i64> {
    match value {
        Some(Value::Number(number)) => number.as_i64(),
        Some(Value::String(text)) => text.parse::<i64>().ok(),
        _ => None,
    }
}

pub fn safe_f64(value: Option<&Value>) -> Option<f64> {
    match value {
        Some(Value::Number(number)) => number.as_f64(),
        Some(Value::String(text)) => text.parse::<f64>().ok(),
        _ => None,
    }
}

pub fn redact(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut output = serde_json::Map::new();
            for (key, item) in map {
                if looks_secret_key(key) {
                    output.insert(key.clone(), Value::String("[REDACTED]".to_string()));
                } else {
                    output.insert(key.clone(), redact(item));
                }
            }
            Value::Object(output)
        }
        Value::Array(items) => Value::Array(items.iter().map(redact).collect()),
        Value::String(text) => Value::String(redact_string(text)),
        _ => value.clone(),
    }
}

fn looks_secret_key(key: &str) -> bool {
    let lowered = key.to_ascii_lowercase();
    [
        "authorization",
        "api_key",
        "api-key",
        "access_token",
        "access-token",
        "refresh_token",
        "secret",
        "password",
        "token",
    ]
    .iter()
    .any(|needle| lowered.contains(needle))
}

fn redact_string(text: &str) -> String {
    if text.starts_with("sk-") || text.starts_with("ghp_") || text.starts_with("gho_") {
        "[REDACTED]".to_string()
    } else {
        text.to_string()
    }
}

pub fn git_context(cwd: Option<&str>) -> (Option<String>, Option<String>) {
    let Some(cwd) = cwd else {
        return (None, None);
    };
    let path = Path::new(cwd);
    if !path.exists() {
        return (None, None);
    }
    let top = std::process::Command::new("git")
        .args(["-C", cwd, "rev-parse", "--show-toplevel"])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string());
    let branch = std::process::Command::new("git")
        .args(["-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string());
    let repo = top
        .as_deref()
        .filter(|text| !text.is_empty())
        .and_then(|text| Path::new(text).file_name())
        .map(|name| name.to_string_lossy().to_string());
    (repo, branch.filter(|text| !text.is_empty()))
}

pub fn git_repo_state(cwd: Option<&str>) -> Value {
    let Some(cwd) = cwd else {
        return serde_json::json!({});
    };
    let path = Path::new(cwd);
    if !path.exists() {
        return serde_json::json!({"cwd": cwd});
    }
    let mut state = serde_json::json!({"cwd": cwd});
    let top = git_output(cwd, &["rev-parse", "--show-toplevel"]);
    let branch = git_output(cwd, &["rev-parse", "--abbrev-ref", "HEAD"]);
    let sha = git_output(cwd, &["rev-parse", "HEAD"]);
    if let Some(top) = top {
        state["git_root"] = serde_json::json!(top);
    }
    if let Some(branch) = branch {
        state["branch"] = serde_json::json!(branch);
    }
    if let Some(sha) = sha {
        state["sha"] = serde_json::json!(sha);
    }
    if let Some(git_root) = state
        .get("git_root")
        .and_then(Value::as_str)
        .map(ToString::to_string)
    {
        let status = std::process::Command::new("git")
            .args(["-C", &git_root, "status", "--short"])
            .output()
            .ok()
            .filter(|output| output.status.success())
            .map(|output| String::from_utf8_lossy(&output.stdout).to_string())
            .unwrap_or_default();
        let dirty = !status.trim().is_empty();
        state["dirty"] = serde_json::json!(dirty);
        if dirty {
            state["dirty_status"] = serde_json::json!(status);
            if let Some(patch) = std::process::Command::new("git")
                .args(["-C", &git_root, "diff", "--binary"])
                .output()
                .ok()
                .filter(|output| output.status.success())
                .map(|output| String::from_utf8_lossy(&output.stdout).to_string())
                .filter(|patch| !patch.trim().is_empty())
            {
                state["dirty_patch"] = serde_json::json!(patch);
            }
        }
    }
    state
}

fn git_output(cwd: &str, args: &[&str]) -> Option<String> {
    let output = std::process::Command::new("git")
        .arg("-C")
        .arg(cwd)
        .args(args)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if text.is_empty() { None } else { Some(text) }
}

pub fn command_join(parts: &[String]) -> String {
    if cfg!(windows) {
        parts
            .iter()
            .map(|part| quote_windows_arg(part))
            .collect::<Vec<_>>()
            .join(" ")
    } else {
        parts
            .iter()
            .map(|part| quote_posix_arg(part))
            .collect::<Vec<_>>()
            .join(" ")
    }
}

pub struct CommandResult {
    pub status: i32,
    pub stdout: String,
    pub stderr: String,
}

pub fn run_shell_command(
    command: &str,
    cwd: Option<&Path>,
    envs: &[(&str, String)],
    input: Option<&str>,
) -> Result<CommandResult, String> {
    let mut cmd = if cfg!(windows) {
        let mut cmd = Command::new("powershell.exe");
        cmd.args([
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ]);
        cmd
    } else {
        let mut cmd = Command::new("sh");
        cmd.args(["-c", command]);
        cmd
    };
    if let Some(cwd) = cwd {
        cmd.current_dir(cwd);
    }
    for (key, value) in envs {
        cmd.env(key, value);
    }
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    if input.is_some() {
        cmd.stdin(Stdio::piped());
    }
    let mut child = cmd.spawn().map_err(|err| err.to_string())?;
    if let Some(input) = input {
        if let Some(mut stdin) = child.stdin.take() {
            stdin
                .write_all(input.as_bytes())
                .map_err(|err| err.to_string())?;
        }
    }
    let output = child.wait_with_output().map_err(|err| err.to_string())?;
    Ok(CommandResult {
        status: output.status.code().unwrap_or(-1),
        stdout: String::from_utf8_lossy(&output.stdout).to_string(),
        stderr: String::from_utf8_lossy(&output.stderr).to_string(),
    })
}

fn quote_posix_arg(value: &str) -> String {
    if value.is_empty() {
        return "''".to_string();
    }
    if value
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || "-_./:=+".contains(ch))
    {
        value.to_string()
    } else {
        format!("'{}'", value.replace('\'', "'\\''"))
    }
}

fn quote_windows_arg(value: &str) -> String {
    if value.is_empty() {
        return "\"\"".to_string();
    }
    if !value.chars().any(|ch| ch.is_whitespace() || ch == '"') {
        return value.to_string();
    }
    let mut result = String::from("\"");
    let mut backslashes = 0;
    for ch in value.chars() {
        if ch == '\\' {
            backslashes += 1;
        } else if ch == '"' {
            result.push_str(&"\\".repeat(backslashes * 2 + 1));
            result.push('"');
            backslashes = 0;
        } else {
            result.push_str(&"\\".repeat(backslashes));
            backslashes = 0;
            result.push(ch);
        }
    }
    result.push_str(&"\\".repeat(backslashes * 2));
    result.push('"');
    result
}
