from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .tailer import SQLITE_SUFFIXES, iter_ingestable_files, parse_possible_json


def audit_codex_paths(paths: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "paths": [],
        "files": 0,
        "sqlite_files": 0,
        "json_files": 0,
        "tables": {},
        "event_hints": {},
        "fields": {},
        "errors": [],
    }
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        path_info = {"path": str(path), "exists": path.exists()}
        report["paths"].append(path_info)
        if not path.exists():
            continue
        for file_path in iter_ingestable_files(path):
            report["files"] += 1
            if file_path.suffix.lower() in SQLITE_SUFFIXES:
                report["sqlite_files"] += 1
                audit_sqlite_file(file_path, report)
            else:
                report["json_files"] += 1
                audit_json_file(file_path, report)
    report["coverage"] = coverage_summary(report)
    return report


def audit_sqlite_file(path: Path, report: dict[str, Any]) -> None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        report["errors"].append({"path": str(path), "error": str(exc)})
        return
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for table_row in rows:
            table = table_row["name"]
            key = f"{path.name}:{table}"
            count = conn.execute(f"SELECT COUNT(*) AS count FROM {quote_identifier(table)}").fetchone()["count"]
            columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()]
            report["tables"][key] = {"rows": int(count or 0), "columns": columns}
            sample_rows = conn.execute(f"SELECT * FROM {quote_identifier(table)} LIMIT 100").fetchall()
            for sample in sample_rows:
                payload = sqlite_row_payload(sample)
                observe_payload(payload, report)
    except sqlite3.Error as exc:
        report["errors"].append({"path": str(path), "error": str(exc)})
    finally:
        conn.close()


def audit_json_file(path: Path, report: dict[str, Any]) -> None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= 100:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    observe_payload(payload, report)
    except OSError as exc:
        report["errors"].append({"path": str(path), "error": str(exc)})


def sqlite_row_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            parsed = parse_possible_json(value)
            if isinstance(parsed, dict):
                payload.update(parsed)
            else:
                payload[key] = value
        elif value is not None:
            payload[key] = value
    return payload


def observe_payload(payload: dict[str, Any], report: dict[str, Any]) -> None:
    hint = event_hint(payload)
    report["event_hints"][hint] = int(report["event_hints"].get(hint, 0)) + 1
    for field in payload.keys():
        report["fields"][field] = int(report["fields"].get(field, 0)) + 1


def event_hint(payload: dict[str, Any]) -> str:
    value = payload.get("event_type") or payload.get("hook_event_name") or payload.get("type")
    if value:
        return str(value)
    if payload.get("tool_name") or payload.get("tool"):
        return "tool"
    if payload.get("prompt"):
        return "prompt"
    message = payload.get("message")
    if isinstance(message, dict) and message.get("role"):
        return str(message.get("role"))
    return "unknown"


def coverage_summary(report: dict[str, Any]) -> dict[str, bool]:
    fields = set(report.get("fields") or {})
    hints = {str(key).lower() for key in (report.get("event_hints") or {})}
    return {
        "has_prompt": bool({"prompt", "user_prompt", "input"} & fields or {"user", "prompt"} & hints),
        "has_tool": bool({"tool", "tool_name", "toolName"} & fields or "tool" in hints),
        "has_usage": bool({"usage", "cost_usd", "cost_usd_est", "total_cost_usd", "input_tokens", "output_tokens"} & fields),
        "has_timestamps": bool({"ts", "timestamp", "created_at", "time"} & fields),
        "has_sessions": bool({"session_id", "sessionId", "conversation_id", "rollout_id"} & fields),
    }


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
