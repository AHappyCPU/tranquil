use serde_json::{Value, json};

use crate::http::post_json;
use crate::storage::Storage;
use crate::util::now_ts;

pub struct SyncResult {
    pub status: u16,
    pub events: usize,
    pub runs: usize,
    pub fixtures: usize,
    pub scores: usize,
}

pub fn push_sync(
    storage: &Storage,
    endpoint: &str,
    headers: &[(String, String)],
) -> Result<SyncResult, String> {
    let data = storage.export_data()?;
    let events = len(&data, "events");
    let runs = len(&data, "runs");
    let fixtures = len(&data, "fixtures");
    let scores = len(&data, "scores");
    let payload = json!({
        "schema": "tranquil.sync/v1",
        "exported_at": now_ts(),
        "data": data,
    });
    let response = post_json(
        endpoint,
        headers,
        serde_json::to_string(&payload).map_err(|err| err.to_string())?,
        10,
    )?;
    Ok(SyncResult {
        status: response.status,
        events,
        runs,
        fixtures,
        scores,
    })
}

fn len(value: &Value, key: &str) -> usize {
    value
        .get(key)
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0)
}
