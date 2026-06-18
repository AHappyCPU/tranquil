use std::collections::BTreeSet;

use serde_json::Value;

use crate::config::Config;
use crate::http::post_json;
use crate::storage::Storage;
use crate::util::run_shell_command;

pub fn notify_new_signals(
    storage: &Storage,
    config: &Config,
    run_id: &str,
    before_signal_ids: &BTreeSet<String>,
) -> Result<(), String> {
    if !enabled(config) {
        return Ok(());
    }
    for signal in storage.list_run_signals(run_id)? {
        if before_signal_ids.contains(&signal.signal_id) {
            continue;
        }
        let signal = serde_json::to_value(&signal).map_err(|err| err.to_string())?;
        deliver_sync(config, &signal)?;
    }
    Ok(())
}

pub fn signal_ids(storage: &Storage, run_id: &str) -> Result<BTreeSet<String>, String> {
    Ok(storage
        .list_run_signals(run_id)?
        .into_iter()
        .map(|signal| signal.signal_id)
        .collect())
}

fn deliver_sync(config: &Config, signal: &Value) -> Result<(), String> {
    let body = serde_json::to_string(&serde_json::json!({
        "type": "signal",
        "signal": signal,
    }))
    .map_err(|err| err.to_string())?;
    if let Some(url) = &config.notification_webhook_url {
        post_json(url, &[], body.clone(), 3)?;
    }
    if let Some(command) = &config.notification_command {
        run_shell_command(command, None, &[], Some(&body))?;
    }
    Ok(())
}

fn enabled(config: &Config) -> bool {
    config.notification_webhook_url.is_some() || config.notification_command.is_some()
}
