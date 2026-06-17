from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .normalize import normalize_event
from .storage import Storage


JSON_SUFFIXES = {".jsonl", ".json", ".log"}
SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}


def ingest_path(storage: Storage, path: str | Path, agent: str | None = None, limit: int | None = None) -> int:
    target = Path(path).expanduser()
    if not target.exists():
        raise FileNotFoundError(target)
    count = 0
    for file_path in iter_ingestable_files(target):
        if limit is not None and count >= limit:
            break
        if file_path.suffix.lower() in SQLITE_SUFFIXES:
            count += ingest_sqlite(storage, file_path, agent=agent, limit=None if limit is None else limit - count)
        else:
            count += ingest_json_lines(storage, file_path, agent=agent, limit=None if limit is None else limit - count)
    return count


def iter_ingestable_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    for child in sorted(path.rglob("*")):
        if child.is_file() and (child.suffix.lower() in JSON_SUFFIXES or child.suffix.lower() in SQLITE_SUFFIXES):
            yield child


def ingest_json_lines(storage: Storage, path: Path, agent: str | None = None, limit: int | None = None, start_offset: int = 0) -> int:
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        if start_offset:
            handle.seek(start_offset)
        for line in handle:
            if limit is not None and count >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if agent and "agent" not in payload:
                payload["agent"] = agent
            if not any(key in payload for key in ("ts", "timestamp", "created_at", "time")):
                payload["timestamp"] = path.stat().st_mtime
            event_hint = infer_event_hint(payload)
            event = normalize_event(event_hint, payload, source="transcript")
            if storage.record_event(event):
                count += 1
    return count


def ingest_sqlite(storage: Storage, path: Path, agent: str | None = None, limit: int | None = None) -> int:
    count = 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        ]
        for table in tables:
            columns = conn.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()
            text_columns = [row["name"] for row in columns if str(row["type"]).upper() in {"", "TEXT", "JSON", "BLOB"}]
            if not text_columns:
                continue
            rows = conn.execute(f"SELECT * FROM {quote_identifier(table)}").fetchall()
            for row in rows:
                if limit is not None and count >= limit:
                    return count
                payload = row_to_payload(row, text_columns)
                if not payload:
                    continue
                payload.setdefault("agent", agent or "codex")
                payload.setdefault("rollout_table", table)
                event = normalize_event(infer_event_hint(payload), payload, source="transcript")
                if storage.record_event(event):
                    count += 1
    finally:
        conn.close()
    return count


def row_to_payload(row: sqlite3.Row, text_columns: list[str]) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    for column in row.keys():
        value = row[column]
        if value is None:
            continue
        if column in text_columns and isinstance(value, (str, bytes)):
            text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
            parsed = parse_possible_json(text)
            if isinstance(parsed, dict):
                merged.update(parsed)
            elif column.lower() in {"message", "content", "text", "prompt"}:
                merged[column] = text
        elif isinstance(value, (str, int, float)):
            merged[column] = value
    return merged or None


def parse_possible_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def infer_event_hint(payload: dict[str, Any]) -> str:
    explicit = payload.get("event_type") or payload.get("hook_event_name") or payload.get("type")
    if explicit:
        role = str(explicit).lower()
        if role in {"user", "user_message"}:
            return "user-prompt-submit"
        if role in {"assistant", "assistant_message"}:
            return "stop"
        return str(explicit)
    message = payload.get("message")
    if isinstance(message, dict):
        role = str(message.get("role") or "").lower()
        if role == "user":
            return "user-prompt-submit"
        if role == "assistant":
            return "stop"
    if payload.get("prompt"):
        return "user-prompt-submit"
    if payload.get("tool_name") or payload.get("tool"):
        return "post-tool-use"
    return "event"


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class TranscriptTailer(threading.Thread):
    def __init__(self, storage: Storage, paths: list[str], interval_seconds: float = 2.0):
        super().__init__(name="tranquil-transcript-tailer", daemon=True)
        self.storage = storage
        self.paths = [Path(path).expanduser() for path in paths]
        self.interval_seconds = max(0.5, interval_seconds)
        self.offsets: dict[Path, int] = {}
        self.errors: list[str] = []
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # pragma: no cover - visible through health if needed
                self.errors.append(f"{type(exc).__name__}: {exc}")
                del self.errors[:-20]
            self._stop_event.wait(self.interval_seconds)

    def scan_once(self) -> int:
        count = 0
        for root in self.paths:
            if not root.exists():
                continue
            for path in iter_ingestable_files(root):
                if path.suffix.lower() not in JSON_SUFFIXES:
                    continue
                offset = self.offsets.get(path, 0)
                size = path.stat().st_size
                if size < offset:
                    offset = 0
                if size == offset:
                    continue
                count += ingest_json_lines(self.storage, path, start_offset=offset)
                self.offsets[path] = path.stat().st_size
        return count


class RolloutTailer(threading.Thread):
    def __init__(self, storage: Storage, paths: list[str], interval_seconds: float = 2.0):
        super().__init__(name="tranquil-rollout-tailer", daemon=True)
        self.storage = storage
        self.paths = [Path(path).expanduser() for path in paths]
        self.interval_seconds = max(0.5, interval_seconds)
        self.offsets: dict[tuple[Path, str], int] = {}
        self.errors: list[str] = []
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # pragma: no cover - visible through health if needed
                self.errors.append(f"{type(exc).__name__}: {exc}")
                del self.errors[:-20]
            self._stop_event.wait(self.interval_seconds)

    def scan_once(self) -> int:
        count = 0
        for root in self.paths:
            if not root.exists():
                continue
            for path in iter_ingestable_files(root):
                if path.suffix.lower() not in SQLITE_SUFFIXES:
                    continue
                count += self.ingest_new_sqlite_rows(path)
        return count

    def ingest_new_sqlite_rows(self, path: Path) -> int:
        count = 0
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = [
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            ]
            for table in tables:
                columns = conn.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()
                text_columns = [row["name"] for row in columns if str(row["type"]).upper() in {"", "TEXT", "JSON", "BLOB"}]
                if not text_columns:
                    self.offsets[(path, table)] = table_row_count(conn, table)
                    continue
                offset_key = (path, table)
                offset = self.offsets.get(offset_key, 0)
                total = table_row_count(conn, table)
                if total < offset:
                    offset = 0
                if total == offset:
                    continue
                rows = conn.execute(f"SELECT * FROM {quote_identifier(table)} LIMIT -1 OFFSET ?", (offset,)).fetchall()
                for row in rows:
                    payload = row_to_payload(row, text_columns)
                    if not payload:
                        continue
                    payload.setdefault("agent", "codex")
                    payload.setdefault("rollout_table", table)
                    event = normalize_event(infer_event_hint(payload), payload, source="transcript")
                    if self.storage.record_event(event):
                        count += 1
                self.offsets[offset_key] = total
        finally:
            conn.close()
        return count


def table_row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS count FROM {quote_identifier(table)}").fetchone()["count"] or 0)
