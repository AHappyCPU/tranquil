use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::util::{default_home, expand_home, new_id};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub home: PathBuf,
    pub db_path: PathBuf,
    pub token: String,
    pub raw_payloads: bool,
    pub signal_thresholds: SignalThresholds,
    pub kill_switch_enabled: bool,
    pub run_cost_budget_usd: f64,
    pub policy_enabled: bool,
    pub policy_forbidden_paths: Vec<String>,
    pub policy_forbidden_commands: Vec<String>,
    pub codex_rollout_paths: Vec<String>,
    pub transcript_paths: Vec<String>,
    pub tail_interval_seconds: f64,
    pub notification_webhook_url: Option<String>,
    pub notification_command: Option<String>,
    pub replay_command: Option<String>,
    pub judge_command: Option<String>,
    pub trace_sampling_enabled: bool,
    pub trace_sample_rate: f64,
    pub trace_sample_suite: String,
    pub sync_endpoint: Option<String>,
    pub sync_headers: std::collections::BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
struct ConfigFile {
    db_path: Option<String>,
    token: Option<String>,
    raw_payloads: Option<bool>,
    signal_thresholds: Option<SignalThresholdsFile>,
    kill_switch_enabled: Option<bool>,
    run_cost_budget_usd: Option<f64>,
    policy_enabled: Option<bool>,
    policy_forbidden_paths: Option<Vec<String>>,
    policy_forbidden_commands: Option<Vec<String>>,
    codex_rollout_paths: Option<Vec<String>>,
    transcript_paths: Option<Vec<String>>,
    tail_interval_seconds: Option<f64>,
    notification_webhook_url: Option<String>,
    notification_command: Option<String>,
    replay_command: Option<String>,
    judge_command: Option<String>,
    trace_sampling_enabled: Option<bool>,
    trace_sample_rate: Option<f64>,
    trace_sample_suite: Option<String>,
    sync_endpoint: Option<String>,
    sync_headers: Option<std::collections::BTreeMap<String, String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SignalThresholds {
    pub loop_repeats: i64,
    pub reread_repeats: i64,
    pub failure_cascade_count: i64,
    pub runaway_cost_usd: f64,
    pub runaway_cost_per_min_usd: f64,
    pub idle_minutes: i64,
    pub scheduled_idle_minutes: i64,
}

#[derive(Debug, Deserialize)]
struct SignalThresholdsFile {
    loop_repeats: Option<i64>,
    reread_repeats: Option<i64>,
    failure_cascade_count: Option<i64>,
    runaway_cost_usd: Option<f64>,
    runaway_cost_per_min_usd: Option<f64>,
    idle_minutes: Option<i64>,
    scheduled_idle_minutes: Option<i64>,
}

impl Default for SignalThresholds {
    fn default() -> Self {
        Self {
            loop_repeats: 3,
            reread_repeats: 5,
            failure_cascade_count: 3,
            runaway_cost_usd: 5.0,
            runaway_cost_per_min_usd: 2.0,
            idle_minutes: 20,
            scheduled_idle_minutes: 10,
        }
    }
}

impl Config {
    pub fn load(home: Option<PathBuf>, create: bool) -> Result<Self, String> {
        let home = home.unwrap_or_else(default_home);
        let home = expand_home(&home.to_string_lossy());
        let path = home.join("config.json");
        let mut config = Self {
            home: home.clone(),
            db_path: home.join("tranquil.db"),
            token: std::env::var("TRANQUIL_TOKEN").unwrap_or_else(|_| new_id("tok")),
            raw_payloads: true,
            signal_thresholds: SignalThresholds::default(),
            kill_switch_enabled: false,
            run_cost_budget_usd: 10.0,
            policy_enabled: false,
            policy_forbidden_paths: Vec::new(),
            policy_forbidden_commands: Vec::new(),
            codex_rollout_paths: default_codex_rollout_paths(),
            transcript_paths: default_transcript_paths(),
            tail_interval_seconds: 2.0,
            notification_webhook_url: std::env::var("TRANQUIL_NOTIFICATION_WEBHOOK_URL").ok(),
            notification_command: std::env::var("TRANQUIL_NOTIFICATION_COMMAND").ok(),
            replay_command: None,
            judge_command: std::env::var("TRANQUIL_JUDGE_COMMAND").ok(),
            trace_sampling_enabled: env_bool("TRANQUIL_TRACE_SAMPLING_ENABLED", false),
            trace_sample_rate: std::env::var("TRANQUIL_TRACE_SAMPLE_RATE")
                .ok()
                .and_then(|value| value.parse().ok())
                .unwrap_or(0.05),
            trace_sample_suite: std::env::var("TRANQUIL_TRACE_SAMPLE_SUITE")
                .unwrap_or_else(|_| "sampled".to_string()),
            sync_endpoint: std::env::var("TRANQUIL_SYNC_ENDPOINT").ok(),
            sync_headers: std::collections::BTreeMap::new(),
        };
        if path.exists() {
            let text = std::fs::read_to_string(&path).map_err(|err| err.to_string())?;
            let parsed: ConfigFile = serde_json::from_str(text.trim_start_matches('\u{feff}'))
                .map_err(|err| err.to_string())?;
            if let Some(db_path) = parsed.db_path {
                config.db_path = expand_home(&db_path);
            }
            if let Some(token) = std::env::var("TRANQUIL_TOKEN").ok().or(parsed.token) {
                config.token = token;
            }
            if let Some(raw_payloads) = parsed.raw_payloads {
                config.raw_payloads = raw_payloads;
            }
            if let Some(thresholds) = parsed.signal_thresholds {
                config.signal_thresholds = SignalThresholds {
                    loop_repeats: thresholds
                        .loop_repeats
                        .unwrap_or(config.signal_thresholds.loop_repeats),
                    reread_repeats: thresholds
                        .reread_repeats
                        .unwrap_or(config.signal_thresholds.reread_repeats),
                    failure_cascade_count: thresholds
                        .failure_cascade_count
                        .unwrap_or(config.signal_thresholds.failure_cascade_count),
                    runaway_cost_usd: thresholds
                        .runaway_cost_usd
                        .unwrap_or(config.signal_thresholds.runaway_cost_usd),
                    runaway_cost_per_min_usd: thresholds
                        .runaway_cost_per_min_usd
                        .unwrap_or(config.signal_thresholds.runaway_cost_per_min_usd),
                    idle_minutes: thresholds
                        .idle_minutes
                        .unwrap_or(config.signal_thresholds.idle_minutes),
                    scheduled_idle_minutes: thresholds
                        .scheduled_idle_minutes
                        .unwrap_or(config.signal_thresholds.scheduled_idle_minutes),
                };
            }
            if let Some(value) = parsed.kill_switch_enabled {
                config.kill_switch_enabled = value;
            }
            if let Some(value) = parsed.run_cost_budget_usd {
                config.run_cost_budget_usd = value;
            }
            if let Some(value) = parsed.policy_enabled {
                config.policy_enabled = value;
            }
            if let Some(value) = parsed.policy_forbidden_paths {
                config.policy_forbidden_paths = value;
            }
            if let Some(value) = parsed.policy_forbidden_commands {
                config.policy_forbidden_commands = value;
            }
            if let Some(value) = parsed.codex_rollout_paths {
                config.codex_rollout_paths = value
                    .into_iter()
                    .map(|path| expand_home(&path).to_string_lossy().to_string())
                    .collect();
            }
            if let Some(value) = parsed.transcript_paths {
                config.transcript_paths = value
                    .into_iter()
                    .map(|path| expand_home(&path).to_string_lossy().to_string())
                    .collect();
            }
            if let Some(value) = parsed.tail_interval_seconds {
                config.tail_interval_seconds = value;
            }
            if let Some(value) = std::env::var("TRANQUIL_NOTIFICATION_WEBHOOK_URL")
                .ok()
                .or(parsed.notification_webhook_url)
            {
                config.notification_webhook_url = Some(value);
            }
            if let Some(value) = std::env::var("TRANQUIL_NOTIFICATION_COMMAND")
                .ok()
                .or(parsed.notification_command)
            {
                config.notification_command = Some(value);
            }
            if let Some(value) = parsed.replay_command {
                config.replay_command = Some(value);
            }
            if let Some(value) = std::env::var("TRANQUIL_JUDGE_COMMAND")
                .ok()
                .or(parsed.judge_command)
            {
                config.judge_command = Some(value);
            }
            if let Some(value) = parsed.trace_sampling_enabled {
                config.trace_sampling_enabled = env_bool("TRANQUIL_TRACE_SAMPLING_ENABLED", value);
            }
            if let Some(value) = std::env::var("TRANQUIL_TRACE_SAMPLE_RATE")
                .ok()
                .and_then(|value| value.parse().ok())
                .or(parsed.trace_sample_rate)
            {
                config.trace_sample_rate = value;
            }
            if let Some(value) = std::env::var("TRANQUIL_TRACE_SAMPLE_SUITE")
                .ok()
                .or(parsed.trace_sample_suite)
            {
                config.trace_sample_suite = value;
            }
            if let Some(value) = std::env::var("TRANQUIL_SYNC_ENDPOINT")
                .ok()
                .or(parsed.sync_endpoint)
            {
                config.sync_endpoint = Some(value);
            }
            if let Some(value) = parsed.sync_headers {
                config.sync_headers = value;
            }
        } else if create {
            Self::save(&config)?;
        }
        Ok(config)
    }

    pub fn save(config: &Self) -> Result<(), String> {
        std::fs::create_dir_all(&config.home).map_err(|err| err.to_string())?;
        if let Some(parent) = config.db_path.parent() {
            std::fs::create_dir_all(parent).map_err(|err| err.to_string())?;
        }
        let payload = serde_json::json!({
            "db_path": config.db_path,
            "raw_payloads": config.raw_payloads,
            "token": config.token,
            "kill_switch_enabled": config.kill_switch_enabled,
            "run_cost_budget_usd": config.run_cost_budget_usd,
            "policy_enabled": config.policy_enabled,
            "policy_forbidden_paths": config.policy_forbidden_paths,
            "policy_forbidden_commands": config.policy_forbidden_commands,
            "codex_rollout_paths": config.codex_rollout_paths,
            "transcript_paths": config.transcript_paths,
            "tail_interval_seconds": config.tail_interval_seconds,
            "notification_webhook_url": config.notification_webhook_url,
            "notification_command": config.notification_command,
            "replay_command": config.replay_command,
            "judge_command": config.judge_command,
            "trace_sampling_enabled": config.trace_sampling_enabled,
            "trace_sample_rate": config.trace_sample_rate,
            "trace_sample_suite": config.trace_sample_suite,
            "sync_endpoint": config.sync_endpoint,
            "sync_headers": config.sync_headers,
            "signal_thresholds": config.signal_thresholds,
        });
        std::fs::write(
            config.config_path(),
            serde_json::to_string_pretty(&payload).map_err(|err| err.to_string())? + "\n",
        )
        .map_err(|err| err.to_string())
    }

    pub fn config_path(&self) -> PathBuf {
        self.home.join("config.json")
    }
}

fn default_codex_rollout_paths() -> Vec<String> {
    ["~/.codex", "~/.codex/sessions"]
        .iter()
        .map(|path| expand_home(path).to_string_lossy().to_string())
        .collect()
}

fn default_transcript_paths() -> Vec<String> {
    ["~/.claude/projects"]
        .iter()
        .map(|path| expand_home(path).to_string_lossy().to_string())
        .collect()
}

fn env_bool(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(value) => !matches!(
            value.to_ascii_lowercase().as_str(),
            "0" | "false" | "no" | "off" | ""
        ),
        Err(_) => default,
    }
}
