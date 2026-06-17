from __future__ import annotations

import json
import urllib.request
from typing import Any

from .storage import Storage
from .util import json_loads, parse_iso


def build_otlp_logs_payload(storage: Storage) -> dict[str, Any]:
    data = storage.export_data()
    records = []
    for row in data["events"]:
        event = json_loads(row.get("raw_json"), {})
        ts = event.get("ts") or row.get("ts")
        record: dict[str, Any] = {
            "timeUnixNano": str(to_unix_nano(ts)),
            "severityText": severity_for_event(event.get("event_type")),
            "body": {"stringValue": event.get("message") or event.get("event_type") or "tranquil.event"},
            "attributes": attributes(
                {
                    "tranquil.event_id": event.get("event_id") or row.get("event_id"),
                    "tranquil.run_id": event.get("run_id") or row.get("run_id"),
                    "tranquil.session_id": event.get("session_id") or row.get("session_id"),
                    "tranquil.source": event.get("source") or row.get("source"),
                    "tranquil.agent": event.get("agent") or row.get("agent"),
                    "tranquil.event_type": event.get("event_type") or row.get("event_type"),
                    "tranquil.repo": (event.get("context") or {}).get("repo") or row.get("repo"),
                    "tranquil.branch": (event.get("context") or {}).get("branch") or row.get("branch"),
                    "tranquil.model": event.get("resolved_model") or event.get("model") or row.get("resolved_model"),
                    "tranquil.tool": (event.get("tool") or {}).get("name") or row.get("tool_name"),
                    "tranquil.cost_usd_est": (event.get("usage") or {}).get("cost_usd_est") or row.get("cost_usd_est"),
                }
            ),
        }
        records.append(record)
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": attributes(
                        {
                            "service.name": "tranquil",
                            "service.version": "0.1.0",
                        }
                    )
                },
                "scopeLogs": [
                    {
                        "scope": {"name": "tranquil.export", "version": "0.1.0"},
                        "logRecords": records,
                    }
                ],
            }
        ]
    }


def export_otlp_http(storage: Storage, endpoint: str, headers: dict[str, str] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    payload = build_otlp_logs_payload(storage)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **(headers or {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        status = response.status
    return {
        "status": status,
        "records": len(payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]),
        "response": body,
    }


def to_unix_nano(ts: str | None) -> int:
    if not ts:
        return 0
    return int(parse_iso(ts).timestamp() * 1_000_000_000)


def severity_for_event(event_type: str | None) -> str:
    if event_type in {"tool_failure", "permission_denied"}:
        return "ERROR"
    if event_type in {"permission_request"}:
        return "WARN"
    return "INFO"


def attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for key, value in values.items():
        if value is None:
            continue
        result.append({"key": key, "value": otlp_value(value)})
    return result


def otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}

