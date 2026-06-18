use serde_json::{Value, json};

use crate::http::post_json;
use crate::storage::Storage;

pub struct OtlpExportResult {
    pub status: u16,
    pub records: usize,
}

pub fn build_otlp_logs_payload(storage: &Storage) -> Result<Value, String> {
    let data = storage.export_data()?;
    let records = data
        .get("events")
        .and_then(Value::as_array)
        .unwrap_or(&Vec::new())
        .iter()
        .map(log_record)
        .collect::<Vec<_>>();
    Ok(json!({
        "resourceLogs": [
            {
                "resource": {
                    "attributes": attributes(vec![
                        ("service.name", json!("tranquil")),
                        ("service.version", json!("0.1.0")),
                    ])
                },
                "scopeLogs": [
                    {
                        "scope": {"name": "tranquil.export", "version": "0.1.0"},
                        "logRecords": records,
                    }
                ]
            }
        ]
    }))
}

pub fn export_otlp_http(
    storage: &Storage,
    endpoint: &str,
    headers: &[(String, String)],
) -> Result<OtlpExportResult, String> {
    let payload = build_otlp_logs_payload(storage)?;
    let records = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        .as_array()
        .map(Vec::len)
        .unwrap_or(0);
    let response = post_json(
        endpoint,
        headers,
        serde_json::to_string(&payload).map_err(|err| err.to_string())?,
        10,
    )?;
    Ok(OtlpExportResult {
        status: response.status,
        records,
    })
}

fn log_record(event: &Value) -> Value {
    let context = event.get("context").unwrap_or(&Value::Null);
    let tool = event.get("tool").unwrap_or(&Value::Null);
    let usage = event.get("usage").unwrap_or(&Value::Null);
    let event_type = event.get("event_type").and_then(Value::as_str);
    json!({
        "timeUnixNano": to_unix_nano(event.get("ts")).to_string(),
        "severityText": severity_for_event(event_type),
        "body": {
            "stringValue": event
                .get("message")
                .and_then(Value::as_str)
                .or(event_type)
                .unwrap_or("tranquil.event")
        },
        "attributes": attributes(vec![
            ("tranquil.event_id", event.get("event_id").cloned().unwrap_or(Value::Null)),
            ("tranquil.run_id", event.get("run_id").cloned().unwrap_or(Value::Null)),
            ("tranquil.session_id", event.get("session_id").cloned().unwrap_or(Value::Null)),
            ("tranquil.source", event.get("source").cloned().unwrap_or(Value::Null)),
            ("tranquil.agent", event.get("agent").cloned().unwrap_or(Value::Null)),
            ("tranquil.event_type", event.get("event_type").cloned().unwrap_or(Value::Null)),
            ("tranquil.repo", context.get("repo").cloned().unwrap_or(Value::Null)),
            ("tranquil.branch", context.get("branch").cloned().unwrap_or(Value::Null)),
            (
                "tranquil.model",
                event
                    .get("resolved_model")
                    .or_else(|| event.get("model"))
                    .cloned()
                    .unwrap_or(Value::Null),
            ),
            ("tranquil.tool", tool.get("name").cloned().unwrap_or(Value::Null)),
            (
                "tranquil.cost_usd_est",
                usage.get("cost_usd_est").cloned().unwrap_or(Value::Null),
            ),
        ])
    })
}

fn severity_for_event(event_type: Option<&str>) -> &'static str {
    match event_type {
        Some("tool_failure" | "permission_denied") => "ERROR",
        Some("permission_request") => "WARN",
        _ => "INFO",
    }
}

fn attributes(values: Vec<(&str, Value)>) -> Vec<Value> {
    values
        .into_iter()
        .filter(|(_, value)| !value.is_null())
        .map(|(key, value)| json!({"key": key, "value": otlp_value(&value)}))
        .collect()
}

fn otlp_value(value: &Value) -> Value {
    if let Some(value) = value.as_bool() {
        json!({"boolValue": value})
    } else if let Some(value) = value.as_i64() {
        json!({"intValue": value.to_string()})
    } else if let Some(value) = value.as_f64() {
        json!({"doubleValue": value})
    } else if let Some(value) = value.as_str() {
        json!({"stringValue": value})
    } else {
        json!({"stringValue": value.to_string()})
    }
}

fn to_unix_nano(value: Option<&Value>) -> u128 {
    let Some(value) = value else {
        return 0;
    };
    let numeric = value
        .as_f64()
        .or_else(|| value.as_str().and_then(|text| text.parse::<f64>().ok()));
    if let Some(number) = numeric {
        if number > 1_000_000_000_000.0 {
            return (number * 1_000_000.0) as u128;
        }
        return (number * 1_000_000_000.0) as u128;
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::normalize::normalize_event;
    use crate::storage::Storage;

    #[test]
    fn payload_contains_event_records() {
        let root =
            std::env::temp_dir().join(format!("tranquil-rust-otel-{}", crate::util::now_millis()));
        let storage = Storage::open(&root.join("tranquil.db")).unwrap();
        let event = normalize_event(
            "post-tool-use",
            &json!({
                "session_id": "otel",
                "agent": "codex",
                "repo": "repo",
                "branch": "main",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest"},
                "usage": {"cost_usd": 0.1}
            }),
            "hook",
        );
        storage.record_event(&event).unwrap();
        let payload = build_otlp_logs_payload(&storage).unwrap();
        let records = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
            .as_array()
            .unwrap();
        assert_eq!(records.len(), 1);
        let attrs = records[0]["attributes"].as_array().unwrap();
        assert!(attrs.iter().any(|item| {
            item["key"] == "tranquil.agent" && item["value"]["stringValue"] == "codex"
        }));
        assert!(attrs.iter().any(|item| {
            item["key"] == "tranquil.cost_usd_est" && item["value"]["doubleValue"] == 0.1
        }));
        let _ = std::fs::remove_dir_all(root);
    }
}
