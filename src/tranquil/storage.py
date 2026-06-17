from __future__ import annotations

import copy
import difflib
import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .config import SignalThresholds
from .util import (
    command_looks_like_check,
    command_looks_like_pr_or_commit,
    compact_json_for_fingerprint,
    git_repo_state,
    iso_now,
    json_dumps,
    json_loads,
    new_id,
    stable_id,
)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  parent_session_id TEXT,
  depth INTEGER NOT NULL DEFAULT 0,
  agent TEXT NOT NULL,
  agent_version TEXT,
  event_type TEXT NOT NULL,
  source TEXT NOT NULL,
  ts TEXT NOT NULL,
  model TEXT,
  resolved_model TEXT,
  tool_name TEXT,
  duration_ms INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  cost_usd_est REAL,
  permission_decision TEXT,
  repo TEXT,
  branch TEXT,
  cwd TEXT,
  message TEXT,
  raw_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_repo_branch_ts ON events(repo, branch, ts);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_event_type_ts ON events(event_type, ts);
CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  repo TEXT,
  branch TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  last_event_at TEXT NOT NULL,
  status TEXT NOT NULL,
  total_cost_usd_est REAL NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  files_touched INTEGER NOT NULL DEFAULT 0,
  produced_pr INTEGER NOT NULL DEFAULT 0,
  checks_ran INTEGER NOT NULL DEFAULT 0,
  signals_count INTEGER NOT NULL DEFAULT 0,
  first_prompt TEXT,
  latest_message TEXT,
  activity_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_runs_repo_branch_last_event ON runs(repo, branch, last_event_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY,
  signal_key TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  type TEXT NOT NULL,
  severity TEXT NOT NULL,
  fired_at TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  action TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_signals_run_id ON signals(run_id);
CREATE INDEX IF NOT EXISTS idx_signals_active ON signals(active, fired_at);

CREATE TABLE IF NOT EXISTS fixtures (
  fixture_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  suite TEXT NOT NULL DEFAULT 'default',
  prompt TEXT,
  repo_ref_json TEXT NOT NULL,
  recorded_trajectory_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fixtures_suite ON fixtures(suite);

CREATE TABLE IF NOT EXISTS eval_runs (
  eval_run_id TEXT PRIMARY KEY,
  suite TEXT NOT NULL,
  baseline_eval_run_id TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
  score_id TEXT PRIMARY KEY,
  eval_run_id TEXT NOT NULL,
  fixture_id TEXT NOT NULL,
  scorer TEXT NOT NULL,
  value REAL,
  passed INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  FOREIGN KEY(eval_run_id) REFERENCES eval_runs(eval_run_id) ON DELETE CASCADE,
  FOREIGN KEY(fixture_id) REFERENCES fixtures(fixture_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scores_eval_run ON scores(eval_run_id);
"""


class Storage:
    def __init__(
        self,
        db_path: str | Path,
        thresholds: SignalThresholds | None = None,
        raw_payloads: bool = True,
        signal_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.db_path = Path(db_path).expanduser()
        self.thresholds = thresholds or SignalThresholds()
        self.raw_payloads = raw_payloads
        self.signal_sink = signal_sink
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.init_schema()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def record_event(self, event: dict[str, Any], evaluate_signals: bool = True) -> bool:
        tool = event.get("tool") or {}
        usage = event.get("usage") or {}
        permission = event.get("permission") or {}
        context = event.get("context") or {}
        row = {
            "event_id": event["event_id"],
            "run_id": event["run_id"],
            "session_id": event["session_id"],
            "parent_session_id": event.get("parent_session_id"),
            "depth": int(event.get("depth") or 0),
            "agent": event["agent"],
            "agent_version": event.get("agent_version"),
            "event_type": event["event_type"],
            "source": event.get("source", "hook"),
            "ts": event["ts"],
            "model": event.get("model"),
            "resolved_model": event.get("resolved_model"),
            "tool_name": tool.get("name") if isinstance(tool, dict) else None,
            "duration_ms": tool.get("duration_ms") if isinstance(tool, dict) else None,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_tokens"),
            "cache_write_tokens": usage.get("cache_write_tokens"),
            "cost_usd_est": usage.get("cost_usd_est"),
            "permission_decision": permission.get("decision") if isinstance(permission, dict) else None,
            "repo": context.get("repo"),
            "branch": context.get("branch"),
            "cwd": context.get("cwd"),
            "message": event.get("message"),
            "raw_json": json_dumps(self.persistable_event(event)),
            "created_at": iso_now(),
        }
        columns = ",".join(row)
        placeholders = ",".join(f":{key}" for key in row)
        with self._conn:
            duplicate = self._find_reconcilable_event(event)
            if duplicate:
                if should_replace_event(existing_source=duplicate["source"], incoming_source=row["source"]):
                    old_run_id = duplicate["run_id"]
                    self._replace_event_row(duplicate["event_id"], row)
                    self._upsert_run_for_event(event)
                    if old_run_id != event["run_id"]:
                        self.refresh_run_rollups(old_run_id)
                    inserted = True
                else:
                    inserted = False
            else:
                cur = self._conn.execute(
                    f"INSERT OR IGNORE INTO events ({columns}) VALUES ({placeholders})",
                    row,
                )
                inserted = cur.rowcount > 0
                if inserted:
                    self._upsert_run_for_event(event)
        if inserted and evaluate_signals:
            from .signals import evaluate_run_signals

            evaluate_run_signals(self, event["run_id"], self.thresholds)
        return inserted

    def _replace_event_row(self, existing_event_id: str, row: dict[str, Any]) -> None:
        assignments = ",".join(f"{key} = :{key}" for key in row)
        replacement = dict(row)
        replacement["existing_event_id"] = existing_event_id
        self._conn.execute(
            f"UPDATE events SET {assignments} WHERE event_id = :existing_event_id",
            replacement,
        )

    def _find_reconcilable_event(self, event: dict[str, Any]) -> sqlite3.Row | None:
        source = event.get("source", "hook")
        if source not in {"hook", "transcript"}:
            return None
        tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
        tool_name = tool.get("name") if isinstance(tool, dict) else None
        rows = self._conn.execute(
            """
            SELECT event_id, run_id, source, raw_json
              FROM events
             WHERE session_id = ?
               AND ts = ?
               AND event_type = ?
               AND COALESCE(tool_name, '') = COALESCE(?, '')
               AND source IN ('hook', 'transcript')
               AND source <> ?
            """,
            (event["session_id"], event["ts"], event["event_type"], tool_name, source),
        ).fetchall()
        fingerprint = event_reconciliation_fingerprint(event)
        for row in rows:
            existing = json_loads(row["raw_json"], {})
            if event_reconciliation_fingerprint(existing) == fingerprint:
                return row
        return None

    def persistable_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if self.raw_payloads:
            return event
        cleaned = copy.deepcopy(event)
        cleaned["raw"] = {}
        return cleaned

    def _upsert_run_for_event(self, event: dict[str, Any]) -> None:
        run_id = event["run_id"]
        existing = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        status = derive_status(event, existing["status"] if existing else None)
        context = event.get("context") or {}
        first_prompt = event.get("message") if event.get("event_type") == "user_prompt" else None
        latest_message = event.get("message")
        produced_pr = 1 if event_produced_pr(event) else 0
        checks_ran = 1 if event_ran_check(event) else 0
        ended_at = event["ts"] if status in {"completed", "failed"} else None
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO runs (
                  run_id, agent, repo, branch, started_at, ended_at, last_event_at,
                  status, produced_pr, checks_ran, first_prompt, latest_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event["agent"],
                    context.get("repo"),
                    context.get("branch"),
                    event["ts"],
                    ended_at,
                    event["ts"],
                    status,
                    produced_pr,
                    checks_ran,
                    first_prompt,
                    latest_message,
                ),
            )
        else:
            self._conn.execute(
                """
                UPDATE runs
                   SET repo = COALESCE(repo, ?),
                       branch = COALESCE(branch, ?),
                       started_at = CASE WHEN ? < started_at THEN ? ELSE started_at END,
                       ended_at = CASE WHEN ? IS NOT NULL THEN ? ELSE ended_at END,
                       last_event_at = CASE WHEN ? > last_event_at THEN ? ELSE last_event_at END,
                       status = ?,
                       produced_pr = MAX(produced_pr, ?),
                       checks_ran = MAX(checks_ran, ?),
                       first_prompt = COALESCE(first_prompt, ?),
                       latest_message = COALESCE(?, latest_message)
                 WHERE run_id = ?
                """,
                (
                    context.get("repo"),
                    context.get("branch"),
                    event["ts"],
                    event["ts"],
                    ended_at,
                    ended_at,
                    event["ts"],
                    event["ts"],
                    status,
                    produced_pr,
                    checks_ran,
                    first_prompt,
                    latest_message,
                    run_id,
                ),
            )
        self.refresh_run_rollups(run_id)

    def refresh_run_rollups(self, run_id: str) -> None:
        cost = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd_est), 0) AS value FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()["value"]
        tool_calls = self._conn.execute(
            "SELECT COUNT(*) AS value FROM events WHERE run_id = ? AND event_type IN ('post_tool','tool_failure','pre_tool')",
            (run_id,),
        ).fetchone()["value"]
        files = self.files_touched(run_id)
        signals = self._conn.execute(
            "SELECT COUNT(*) AS value FROM signals WHERE run_id = ? AND active = 1",
            (run_id,),
        ).fetchone()["value"]
        activity = self.activity_buckets(run_id)
        with self._conn:
            self._conn.execute(
                """
                UPDATE runs
                   SET total_cost_usd_est = ?,
                       tool_calls = ?,
                       files_touched = ?,
                       signals_count = ?,
                       activity_json = ?
                 WHERE run_id = ?
                """,
                (cost, tool_calls, files, signals, json_dumps(activity), run_id),
            )

    def files_touched(self, run_id: str) -> int:
        rows = self._conn.execute(
            "SELECT raw_json FROM events WHERE run_id = ? AND event_type IN ('post_tool','file_changed')",
            (run_id,),
        ).fetchall()
        paths: set[str] = set()
        for row in rows:
            event = json_loads(row["raw_json"], {})
            paths.update(extract_paths(event))
        return len(paths)

    def file_touch_summary(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT event_id, ts, event_type, tool_name, raw_json
              FROM events
             WHERE run_id = ?
               AND event_type IN ('post_tool','tool_failure','file_changed')
             ORDER BY ts, created_at
            """,
            (run_id,),
        ).fetchall()
        by_path: dict[str, dict[str, Any]] = {}
        for row in rows:
            event = json_loads(row["raw_json"], {})
            paths = extract_paths(event)
            if not paths:
                continue
            access = file_access_kind(event)
            for path in paths:
                summary = by_path.setdefault(
                    path,
                    {
                        "path": path,
                        "reads": 0,
                        "writes": 0,
                        "events": 0,
                        "tools": [],
                        "last_event_at": None,
                        "reread_thrash": False,
                    },
                )
                summary["events"] += 1
                if access == "write":
                    summary["writes"] += 1
                else:
                    summary["reads"] += 1
                tool = row["tool_name"] or event.get("event_type")
                if tool and tool not in summary["tools"]:
                    summary["tools"].append(tool)
                summary["last_event_at"] = row["ts"]
        for summary in by_path.values():
            summary["reread_thrash"] = summary["reads"] >= self.thresholds.reread_repeats
        return sorted(by_path.values(), key=lambda item: (not item["reread_thrash"], item["path"]))

    def activity_buckets(self, run_id: str, buckets: int = 12) -> list[int]:
        rows = self._conn.execute(
            "SELECT ts FROM events WHERE run_id = ? ORDER BY ts",
            (run_id,),
        ).fetchall()
        if not rows:
            return []
        if len(rows) <= buckets:
            return [1 for _ in rows]
        # String timestamps are UTC ISO and sort lexically. A simple ordinal
        # bucket is enough for the sparkline in this local dashboard.
        counts = [0 for _ in range(buckets)]
        for index, _row in enumerate(rows):
            bucket = min(buckets - 1, int(index / max(1, len(rows) - 1) * buckets))
            counts[bucket] += 1
        return counts

    def add_signal(self, run_id: str, signal_type: str, severity: str, evidence: dict[str, Any], action: str | None = None) -> bool:
        key = f"{run_id}:{signal_type}:{evidence.get('fingerprint') or evidence.get('path') or evidence.get('tool') or evidence.get('reason') or 'default'}"
        signal_id = new_id("sig")
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO signals (
                  signal_id, signal_key, run_id, type, severity, fired_at, evidence_json, action, active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (signal_id, key, run_id, signal_type, severity, iso_now(), json_dumps(evidence), action),
            )
            inserted = cur.rowcount > 0
        if inserted:
            self.refresh_run_rollups(run_id)
            self._emit_signal(signal_id)
        return inserted

    def _emit_signal(self, signal_id: str) -> None:
        if not self.signal_sink:
            return
        row = self._conn.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
        if not row:
            return
        try:
            self.signal_sink(decode_signal(row))
        except Exception:
            return

    def list_runs(
        self,
        limit: int = 50,
        status: str | None = None,
        agent: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        labels: str | list[str] | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        filters = []
        for column, value in (("status", status), ("agent", agent), ("repo", repo), ("branch", branch)):
            if value:
                filters.append(f"{column} = ?")
                params.append(value)
        if since:
            filters.append("last_event_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        label_filters = normalize_label_filters(labels)
        if label_filters:
            rows = self._conn.execute(
                f"SELECT * FROM runs {where} ORDER BY last_event_at DESC",
                params,
            ).fetchall()
            runs = [self.enrich_run(decode_run(row)) for row in rows]
            return [run for run in runs if labels_match(run["labels"], label_filters)][:limit]
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM runs {where} ORDER BY last_event_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self.enrich_run(decode_run(row)) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self.enrich_run(decode_run(row)) if row else None

    def enrich_run(self, run: dict[str, Any]) -> dict[str, Any]:
        rollup = self.subagent_rollup(run["run_id"])
        run["subagents_count"] = rollup["subagents_count"]
        run["max_depth"] = rollup["max_depth"]
        run["labels"] = self.run_labels(run["run_id"])
        return run

    def run_labels(self, run_id: str) -> dict[str, list[str]]:
        rows = self._conn.execute("SELECT raw_json FROM events WHERE run_id = ?", (run_id,)).fetchall()
        labels: dict[str, set[str]] = {}
        for row in rows:
            event = json_loads(row["raw_json"], {})
            raw_labels = event.get("labels") if isinstance(event.get("labels"), dict) else {}
            for key, value in raw_labels.items():
                labels.setdefault(str(key), set()).add(str(value))
        return {key: sorted(values) for key, values in sorted(labels.items())}

    def subagent_rollup(self, run_id: str) -> dict[str, int]:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS subagents_count,
                   COALESCE(MAX(depth), 0) AS max_depth
              FROM (
                SELECT session_id,
                       MAX(depth) AS depth,
                       MAX(CASE WHEN parent_session_id IS NOT NULL OR depth > 0 THEN 1 ELSE 0 END) AS is_subagent
                  FROM events
                 WHERE run_id = ?
                 GROUP BY session_id
              )
             WHERE is_subagent = 1
            """,
            (run_id,),
        ).fetchone()
        return {"subagents_count": int(row["subagents_count"] or 0), "max_depth": int(row["max_depth"] or 0)}

    def list_subagents(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT session_id,
                   MAX(parent_session_id) AS parent_session_id,
                   MAX(depth) AS depth,
                   MIN(ts) AS started_at,
                   MAX(ts) AS last_event_at,
                   COALESCE(SUM(cost_usd_est), 0) AS cost_usd_est,
                   COUNT(*) AS event_count,
                   SUM(CASE WHEN event_type IN ('post_tool','tool_failure','pre_tool') THEN 1 ELSE 0 END) AS tool_calls,
                   SUM(CASE WHEN event_type = 'tool_failure' THEN 1 ELSE 0 END) AS failures,
                   SUM(CASE WHEN event_type IN ('session_end','stop','task_completed','subagent_stop') THEN 1 ELSE 0 END) AS completions,
                   COALESCE(MAX(resolved_model), MAX(model)) AS model,
                   MAX(message) AS latest_message
              FROM events
             WHERE run_id = ?
             GROUP BY session_id
            HAVING MAX(CASE WHEN parent_session_id IS NOT NULL OR depth > 0 THEN 1 ELSE 0 END) = 1
             ORDER BY MAX(depth), MIN(ts), session_id
            """,
            (run_id,),
        ).fetchall()
        subagents = []
        for row in rows:
            result = dict(row)
            failures = int(result.pop("failures") or 0)
            completions = int(result.pop("completions") or 0)
            if failures:
                status = "failed"
            elif completions:
                status = "completed"
            else:
                status = "running"
            result["status"] = status
            result["depth"] = int(result.get("depth") or 0)
            result["cost_usd_est"] = float(result.get("cost_usd_est") or 0)
            result["event_count"] = int(result.get("event_count") or 0)
            result["tool_calls"] = int(result.get("tool_calls") or 0)
            subagents.append(result)
        return subagents

    def get_run_events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT raw_json FROM events WHERE run_id = ? ORDER BY ts, created_at",
            (run_id,),
        ).fetchall()
        return [json_loads(row["raw_json"], {}) for row in rows]

    def get_run_display_events(self, run_id: str) -> list[dict[str, Any]]:
        events = self.get_run_events(run_id)
        for event in events:
            diff = event_diff_preview(event)
            if diff:
                event["diff"] = diff
        return events

    def get_recent_events(self, run_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT raw_json FROM events WHERE run_id = ? ORDER BY ts DESC, created_at DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return [json_loads(row["raw_json"], {}) for row in rows]

    def list_signals(self, active: bool | None = True, limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if active is not None:
            where = "WHERE active = ?"
            params.append(1 if active else 0)
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM signals {where} ORDER BY fired_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [decode_signal(row) for row in rows]

    def list_run_signals(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM signals WHERE run_id = ? ORDER BY fired_at DESC",
            (run_id,),
        ).fetchall()
        return [decode_signal(row) for row in rows]

    def has_active_signal(self, run_id: str, signal_type: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM signals WHERE run_id = ? AND type = ? AND active = 1 LIMIT 1",
            (run_id, signal_type),
        ).fetchone()
        return row is not None

    def request_stop(self, run_id: str, reason: str = "user_requested_stop") -> bool:
        if not self.get_run(run_id):
            raise KeyError(f"run not found: {run_id}")
        return self.add_signal(
            run_id,
            "stop_requested",
            "high",
            {
                "reason": reason,
                "fingerprint": "manual_stop",
                "message": "manual stop requested; future pre-tool hooks will be denied",
            },
            action="deny_pre_tool",
        )

    def stats(self) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS runs,
                   COALESCE(SUM(total_cost_usd_est), 0) AS cost,
                   SUM(
                     CASE
                       WHEN status IN ('running','waiting')
                        AND last_event_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-20 minutes')
                       THEN 1 ELSE 0
                     END
                   ) AS live,
                   SUM(CASE WHEN signals_count > 0 THEN 1 ELSE 0 END) AS signaled
              FROM runs
            """
        ).fetchone()
        return {
            "runs": row["runs"] or 0,
            "cost_usd_est": row["cost"] or 0,
            "live": row["live"] or 0,
            "signaled": row["signaled"] or 0,
        }

    def create_fixture(
        self,
        run_id: str,
        suite: str = "default",
        fixture_id: str | None = None,
        cost_budget_usd: float | None = None,
        latency_budget_s: float | None = None,
        forbidden_paths: list[str] | None = None,
        repo_ref_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            raise KeyError(f"run not found: {run_id}")
        events = self.get_run_events(run_id)
        prompt = run.get("first_prompt") or first_user_prompt(events)
        repo_ref = git_repo_state(first_event_cwd(events))
        repo_ref.setdefault("repo", run.get("repo"))
        repo_ref.setdefault("branch", run.get("branch"))
        budgets: dict[str, float] = {}
        if cost_budget_usd is not None:
            budgets["cost_usd"] = float(cost_budget_usd)
        if latency_budget_s is not None:
            budgets["wall_clock_s"] = float(latency_budget_s)
        if budgets:
            repo_ref["budgets"] = budgets
        if forbidden_paths:
            repo_ref["forbidden_paths"] = forbidden_paths
        if repo_ref_extra:
            repo_ref.update(repo_ref_extra)
        fixture_id = fixture_id or new_id("fix")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO fixtures (
                  fixture_id, run_id, suite, prompt, repo_ref_json, recorded_trajectory_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (fixture_id, run_id, suite, prompt, json_dumps(repo_ref), json_dumps(events), iso_now()),
            )
        return self.get_fixture(fixture_id) or {}

    def upsert_fixture_definition(
        self,
        fixture_id: str,
        run_id: str,
        suite: str = "default",
        prompt: str | None = None,
        repo_ref: dict[str, Any] | None = None,
        budgets: dict[str, Any] | None = None,
        forbidden_paths: list[str] | None = None,
        rubric: str | None = None,
        reference: str | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            raise KeyError(f"run not found: {run_id}")
        events = self.get_run_events(run_id)
        prompt = prompt or run.get("first_prompt") or first_user_prompt(events)
        merged_repo_ref = repo_ref.copy() if repo_ref else git_repo_state(first_event_cwd(events))
        merged_repo_ref.setdefault("repo", run.get("repo"))
        merged_repo_ref.setdefault("branch", run.get("branch"))
        if budgets:
            merged_repo_ref["budgets"] = {key: float(value) for key, value in budgets.items()}
        if forbidden_paths:
            merged_repo_ref["forbidden_paths"] = [str(path) for path in forbidden_paths]
        if rubric is not None:
            merged_repo_ref["rubric"] = str(rubric)
        if reference is not None:
            merged_repo_ref["reference"] = str(reference)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO fixtures (
                  fixture_id, run_id, suite, prompt, repo_ref_json, recorded_trajectory_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fixture_id) DO UPDATE SET
                  run_id = excluded.run_id,
                  suite = excluded.suite,
                  prompt = excluded.prompt,
                  repo_ref_json = excluded.repo_ref_json,
                  recorded_trajectory_json = excluded.recorded_trajectory_json
                """,
                (fixture_id, run_id, suite, prompt, json_dumps(merged_repo_ref), json_dumps(events), iso_now()),
            )
        return self.get_fixture(fixture_id) or {}

    def create_fixtures_from_signals(self, suite: str = "signals") -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT s.run_id
              FROM signals s
             WHERE s.active = 1
               AND NOT EXISTS (
                 SELECT 1 FROM fixtures f WHERE f.run_id = s.run_id AND f.suite = ?
               )
             ORDER BY s.fired_at DESC
            """,
            (suite,),
        ).fetchall()
        fixtures = []
        for row in rows:
            fixtures.append(self.create_fixture(row["run_id"], suite=suite))
        return fixtures

    def sample_runs(
        self,
        suite: str = "sampled",
        sample_rate: float = 1.0,
        limit: int = 20,
        status: str = "completed",
        agent: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        labels: str | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        fixtures = []
        candidates = self.list_runs(
            limit=max(limit * 20, limit),
            status=status,
            agent=agent,
            repo=repo,
            branch=branch,
            labels=labels,
        )
        for run in candidates:
            if len(fixtures) >= limit:
                break
            fixture = self.sample_run_if_eligible(run["run_id"], suite=suite, sample_rate=sample_rate, required_status=status)
            if fixture:
                fixtures.append(fixture)
        return fixtures

    def sample_run_if_eligible(
        self,
        run_id: str,
        suite: str = "sampled",
        sample_rate: float = 1.0,
        required_status: str = "completed",
    ) -> dict[str, Any] | None:
        sample_rate = max(0.0, min(1.0, float(sample_rate)))
        if not should_sample_run(run_id, sample_rate):
            return None
        run = self.get_run(run_id)
        if not run or run.get("status") != required_status:
            return None
        if self.fixture_for_run(run_id, suite):
            return None
        fixture_id = stable_id("fix", "sample", suite, run_id)
        return self.create_fixture(
            run_id,
            suite=suite,
            fixture_id=fixture_id,
            repo_ref_extra={"sampled_from": "production_trace", "sample_rate": sample_rate},
        )

    def fixture_for_run(self, run_id: str, suite: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM fixtures WHERE run_id = ? AND suite = ? ORDER BY created_at DESC LIMIT 1",
            (run_id, suite),
        ).fetchone()
        return decode_fixture(row) if row else None

    def get_fixture(self, fixture_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM fixtures WHERE fixture_id = ?", (fixture_id,)).fetchone()
        return decode_fixture(row) if row else None

    def list_fixtures(self, suite: str | None = None) -> list[dict[str, Any]]:
        if suite:
            rows = self._conn.execute("SELECT * FROM fixtures WHERE suite = ? ORDER BY created_at DESC", (suite,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM fixtures ORDER BY created_at DESC").fetchall()
        return [decode_fixture(row) for row in rows]

    def create_eval_run(self, suite: str, status: str = "running", baseline_eval_run_id: str | None = None) -> str:
        eval_run_id = new_id("eval")
        with self._conn:
            self._conn.execute(
                "INSERT INTO eval_runs (eval_run_id, suite, baseline_eval_run_id, started_at, status) VALUES (?, ?, ?, ?, ?)",
                (eval_run_id, suite, baseline_eval_run_id, iso_now(), status),
            )
        return eval_run_id

    def latest_eval_run(self, suite: str, status: str | None = None) -> dict[str, Any] | None:
        params: list[Any] = [suite]
        where = "suite = ?"
        if status:
            where += " AND status = ?"
            params.append(status)
        row = self._conn.execute(
            f"SELECT * FROM eval_runs WHERE {where} ORDER BY ended_at DESC, started_at DESC LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row else None

    def list_eval_runs(self, suite: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if suite:
            where = "WHERE e.suite = ?"
            params.append(suite)
        params.append(limit)
        rows = self._conn.execute(
            f"""
            SELECT e.*,
                   COUNT(s.score_id) AS score_count,
                   COALESCE(SUM(CASE WHEN s.passed = 1 THEN 1 ELSE 0 END), 0) AS passed_count,
                   COALESCE(SUM(CASE WHEN s.passed = 0 THEN 1 ELSE 0 END), 0) AS failed_count
              FROM eval_runs e
              LEFT JOIN scores s ON s.eval_run_id = e.eval_run_id
              {where}
             GROUP BY e.eval_run_id
             ORDER BY COALESCE(e.ended_at, e.started_at) DESC, e.started_at DESC
             LIMIT ?
            """,
            params,
        ).fetchall()
        return [decode_eval_run(row) for row in rows]

    def get_eval_run(self, eval_run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT e.*,
                   COUNT(s.score_id) AS score_count,
                   COALESCE(SUM(CASE WHEN s.passed = 1 THEN 1 ELSE 0 END), 0) AS passed_count,
                   COALESCE(SUM(CASE WHEN s.passed = 0 THEN 1 ELSE 0 END), 0) AS failed_count
              FROM eval_runs e
              LEFT JOIN scores s ON s.eval_run_id = e.eval_run_id
             WHERE e.eval_run_id = ?
             GROUP BY e.eval_run_id
            """,
            (eval_run_id,),
        ).fetchone()
        return decode_eval_run(row) if row else None

    def finish_eval_run(self, eval_run_id: str, status: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE eval_runs SET status = ?, ended_at = ? WHERE eval_run_id = ?",
                (status, iso_now(), eval_run_id),
            )

    def add_score(self, eval_run_id: str, fixture_id: str, scorer: str, value: float | None, passed: bool, detail: dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO scores (score_id, eval_run_id, fixture_id, scorer, value, passed, detail_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (new_id("score"), eval_run_id, fixture_id, scorer, value, 1 if passed else 0, json_dumps(detail)),
            )

    def list_scores(self, eval_run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM scores WHERE eval_run_id = ? ORDER BY fixture_id, scorer",
            (eval_run_id,),
        ).fetchall()
        return [decode_score(row) for row in rows]

    def list_run_scores(self, run_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT s.*,
                   e.suite,
                   e.status AS eval_status,
                   e.started_at AS eval_started_at,
                   e.ended_at AS eval_ended_at
              FROM scores s
              JOIN fixtures f ON f.fixture_id = s.fixture_id
              JOIN eval_runs e ON e.eval_run_id = s.eval_run_id
             WHERE f.run_id = ?
             ORDER BY COALESCE(e.ended_at, e.started_at) DESC, s.scorer
             LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        scores = []
        for row in rows:
            score = decode_score(row)
            score["suite"] = row["suite"]
            score["eval_status"] = row["eval_status"]
            score["eval_started_at"] = row["eval_started_at"]
            score["eval_ended_at"] = row["eval_ended_at"]
            scores.append(score)
        return scores

    def cost_rollup(self, group_by: str = "agent", since: str | None = None) -> list[dict[str, Any]]:
        allowed = {"agent", "repo", "branch", "status"}
        if group_by not in allowed:
            raise ValueError(f"group_by must be one of {', '.join(sorted(allowed))}")
        params: list[Any] = []
        where = ""
        if since:
            where = "WHERE last_event_at >= ?"
            params.append(since)
        rows = self._conn.execute(
            f"""
            SELECT COALESCE({group_by}, 'unknown') AS key,
                   COUNT(*) AS runs,
                   COALESCE(SUM(total_cost_usd_est), 0) AS cost_usd_est
              FROM runs
              {where}
             GROUP BY COALESCE({group_by}, 'unknown')
             ORDER BY cost_usd_est DESC, runs DESC
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def diff_runs(self, a: str, b: str) -> dict[str, Any]:
        run_a = self.get_run(a)
        run_b = self.get_run(b)
        if not run_a or not run_b:
            missing = a if not run_a else b
            raise KeyError(f"run not found: {missing}")
        events_a = self.get_run_events(a)
        events_b = self.get_run_events(b)
        tools_a = [((event.get("tool") or {}).get("name") or event.get("event_type")) for event in events_a]
        tools_b = [((event.get("tool") or {}).get("name") or event.get("event_type")) for event in events_b]
        return {
            "a": run_a,
            "b": run_b,
            "delta": {
                "cost_usd_est": float(run_b["total_cost_usd_est"] or 0) - float(run_a["total_cost_usd_est"] or 0),
                "tool_calls": int(run_b["tool_calls"] or 0) - int(run_a["tool_calls"] or 0),
                "files_touched": int(run_b["files_touched"] or 0) - int(run_a["files_touched"] or 0),
                "signals_count": int(run_b["signals_count"] or 0) - int(run_a["signals_count"] or 0),
            },
            "tools": {
                "a": tools_a,
                "b": tools_b,
            },
        }

    def export_data(self) -> dict[str, Any]:
        return {
            "events": [dict(row) for row in self._conn.execute("SELECT * FROM events ORDER BY ts").fetchall()],
            "runs": [decode_run(row) for row in self._conn.execute("SELECT * FROM runs ORDER BY last_event_at").fetchall()],
            "signals": [decode_signal(row) for row in self._conn.execute("SELECT * FROM signals ORDER BY fired_at").fetchall()],
            "fixtures": [decode_fixture(row) for row in self._conn.execute("SELECT * FROM fixtures ORDER BY created_at").fetchall()],
            "eval_runs": [dict(row) for row in self._conn.execute("SELECT * FROM eval_runs ORDER BY started_at").fetchall()],
            "scores": [decode_score(row) for row in self._conn.execute("SELECT * FROM scores ORDER BY eval_run_id, fixture_id").fetchall()],
        }

    def purge(self, older_than_days: int | None = None, all_data: bool = False) -> dict[str, int]:
        if not all_data and older_than_days is None:
            raise ValueError("purge requires older_than_days or all_data=True")
        counts = {"events": 0, "runs": 0, "signals": 0, "fixtures": 0, "eval_runs": 0, "scores": 0}
        with self._conn:
            if all_data:
                for table in ("scores", "eval_runs", "fixtures", "signals", "events", "runs"):
                    cur = self._conn.execute(f"DELETE FROM {table}")
                    counts[table] = cur.rowcount
                return counts
            cutoff = self._conn.execute(
                "SELECT datetime('now', ?) AS cutoff",
                (f"-{int(older_than_days)} days",),
            ).fetchone()["cutoff"]
            old_run_ids = [
                row["run_id"]
                for row in self._conn.execute(
                    "SELECT run_id FROM runs WHERE last_event_at < strftime('%Y-%m-%dT%H:%M:%fZ', ?)",
                    (cutoff,),
                ).fetchall()
            ]
            for run_id in old_run_ids:
                has_fixture = bool(
                    self._conn.execute("SELECT 1 FROM fixtures WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()
                )
                counts["signals"] += self._conn.execute("DELETE FROM signals WHERE run_id = ?", (run_id,)).rowcount
                counts["events"] += self._conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,)).rowcount
                if has_fixture:
                    self._conn.execute("UPDATE runs SET signals_count = 0 WHERE run_id = ?", (run_id,))
                else:
                    counts["runs"] += self._conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,)).rowcount
        return counts


def derive_status(event: dict[str, Any], current: str | None) -> str:
    event_type = event.get("event_type")
    if event_type == "tool_failure":
        return "failed"
    if event_type == "permission_request":
        return "waiting"
    if event_type == "permission_denied":
        return "waiting"
    if event_type in {"session_end", "stop", "task_completed"}:
        return "completed" if current != "failed" else "failed"
    if current in {"completed", "failed"} and event_type not in {"session_start", "user_prompt"}:
        return current
    return "running"


def event_ran_check(event: dict[str, Any]) -> bool:
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
    command = extract_tool_command(tool)
    return command_looks_like_check(command) if command else False


def event_produced_pr(event: dict[str, Any]) -> bool:
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
    command = extract_tool_command(tool)
    if command and command_looks_like_pr_or_commit(command):
        return True
    message = event.get("message") or ""
    return "pull request" in message.lower() or "pr #" in message.lower()


def extract_tool_command(tool: dict[str, Any]) -> str | None:
    if not tool:
        return None
    tool_input = tool.get("input")
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "script"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
    if isinstance(tool_input, str):
        return tool_input
    return None


def should_replace_event(existing_source: str, incoming_source: str) -> bool:
    return existing_source == "transcript" and incoming_source == "hook"


def event_reconciliation_fingerprint(event: dict[str, Any]) -> str:
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
    permission = event.get("permission") if isinstance(event.get("permission"), dict) else {}
    has_tool = bool(tool)
    return json_dumps(
        {
            "session_id": event.get("session_id"),
            "ts": event.get("ts"),
            "event_type": event.get("event_type"),
            "tool_name": tool.get("name") if isinstance(tool, dict) else None,
            "tool_input": compact_json_for_fingerprint(tool.get("input") if isinstance(tool, dict) else None),
            "message": None if has_tool else compact_json_for_fingerprint(event.get("message")),
            "permission_decision": permission.get("decision") if isinstance(permission, dict) else None,
        }
    )


def normalize_label_filters(labels: str | list[str] | None) -> list[str]:
    if labels is None:
        return []
    if isinstance(labels, str):
        labels = [labels]
    return [label.strip() for label in labels if label and label.strip()]


def labels_match(labels: dict[str, list[str]], filters: list[str]) -> bool:
    for label in filters:
        if "=" in label:
            key, value = label.split("=", 1)
            if value not in labels.get(key, []):
                return False
        elif label not in labels:
            return False
    return True


def should_sample_run(run_id: str, sample_rate: float) -> bool:
    sample_rate = max(0.0, min(1.0, float(sample_rate)))
    if sample_rate <= 0:
        return False
    if sample_rate >= 1:
        return True
    digest = hashlib.sha1(run_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
    return bucket <= sample_rate


def extract_paths(event: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    for source in (event, raw):
        for key in ("file_path", "filePath", "path", "filename"):
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, str) and value:
                paths.add(value)
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
    tool_input = tool.get("input")
    if isinstance(tool_input, dict):
        for key in ("file_path", "filePath", "path", "filename"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                paths.add(value)
    return paths


def file_access_kind(event: dict[str, Any]) -> str:
    if event.get("event_type") == "file_changed":
        return "write"
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
    name = str(tool.get("name") or "").lower()
    write_tools = {
        "write",
        "edit",
        "multiedit",
        "notebookedit",
        "apply_patch",
        "str_replace_editor",
        "str_replace_based_edit_tool",
    }
    return "write" if name in write_tools else "read"


def event_diff_preview(event: dict[str, Any], max_chars: int = 12000) -> dict[str, Any] | None:
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
    tool_input = tool.get("input") if isinstance(tool.get("input"), dict) else {}
    if not isinstance(tool_input, dict):
        return None
    name = str(tool.get("name") or "").lower()
    path = first_path_from_mapping(tool_input)
    if name in {"edit", "str_replace_editor", "str_replace_based_edit_tool"}:
        old = first_string(tool_input, "old_string", "oldString", "old_str", "oldStr", "old")
        new = first_string(tool_input, "new_string", "newString", "new_str", "newStr", "new")
        if old is not None and new is not None:
            text = unified_text_diff(old, new, path)
            return {"kind": "edit", "path": path, **trim_text(text, max_chars)}
    if name == "multiedit":
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            parts = []
            for index, edit in enumerate(edits, start=1):
                if not isinstance(edit, dict):
                    continue
                old = first_string(edit, "old_string", "oldString", "old_str", "oldStr", "old")
                new = first_string(edit, "new_string", "newString", "new_str", "newStr", "new")
                if old is not None and new is not None:
                    parts.append(f"# edit {index}\n{unified_text_diff(old, new, path)}")
            if parts:
                return {"kind": "edit", "path": path, **trim_text("\n".join(parts), max_chars)}
    if name == "write":
        content = first_string(tool_input, "content", "text")
        if content is not None:
            text = unified_text_diff("", content, path, fromfile="/dev/null")
            return {"kind": "write", "path": path, **trim_text(text, max_chars)}
    if name == "apply_patch":
        patch = first_string(tool_input, "patch", "diff")
        if patch:
            return {"kind": "patch", "path": path, **trim_text(patch, max_chars)}
    return None


def unified_text_diff(old: str, new: str, path: str | None, fromfile: str | None = None) -> str:
    label = path or "unknown"
    lines = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=fromfile or f"a/{label}",
        tofile=f"b/{label}",
        lineterm="",
    )
    return "\n".join(lines)


def first_string(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            return value
    return None


def first_path_from_mapping(mapping: dict[str, Any]) -> str | None:
    for key in ("file_path", "filePath", "path", "filename"):
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def trim_text(text: str, max_chars: int) -> dict[str, Any]:
    if len(text) <= max_chars:
        return {"text": text, "truncated": False}
    return {"text": text[:max_chars] + "\n... truncated ...", "truncated": True}


def first_user_prompt(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("event_type") == "user_prompt" and event.get("message"):
            return event["message"]
    return None


def first_event_cwd(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        context = event.get("context") if isinstance(event.get("context"), dict) else {}
        cwd = context.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def decode_run(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["produced_pr"] = bool(result["produced_pr"])
    result["checks_ran"] = bool(result["checks_ran"])
    result["activity"] = json_loads(result.pop("activity_json"), [])
    return result


def decode_signal(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["active"] = bool(result["active"])
    result["evidence"] = json_loads(result.pop("evidence_json"), {})
    return result


def decode_fixture(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["repo_ref"] = json_loads(result.pop("repo_ref_json"), {})
    result["recorded_trajectory"] = json_loads(result.pop("recorded_trajectory_json"), [])
    return result


def decode_eval_run(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in ("score_count", "passed_count", "failed_count"):
        result[key] = int(result.get(key) or 0)
    return result


def decode_score(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["passed"] = bool(result["passed"])
    result["detail"] = json_loads(result.pop("detail_json"), {})
    return result
