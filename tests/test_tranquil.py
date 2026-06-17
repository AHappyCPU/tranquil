from __future__ import annotations

import base64
import contextlib
import os
import json
import io
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tranquil.config import SignalThresholds, TranquilConfig
from tranquil.cli import build_parser, main as tranquil_main, should_launch_after_init
from tranquil.evals import replay_fixture, run_eval, run_eval_matrix
from tranquil.hook_forwarder import main as hook_forward_main
from tranquil.init import run_init
from tranquil.mcp import run_mcp_server
from tranquil.normalize import normalize_event
from tranquil.otel import build_otlp_logs_payload
from tranquil.server import TranquilHTTPServer, pre_tool_decision
from tranquil.storage import Storage
from tranquil.suites import import_fixture_file, import_suite_fixtures, load_suite_file
from tranquil.tailer import RolloutTailer, ingest_path
from tranquil.tui import render_fleet, render_run


class StorageTests(unittest.TestCase):
    def test_ingest_rolls_up_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                event = normalize_event(
                    "post-tool-use",
                    {
                        "agent": "codex",
                        "session_id": "s1",
                        "cwd": tmp,
                        "repo": "repo",
                        "branch": "main",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest"},
                        "usage": {"input_tokens": 10, "output_tokens": 4, "cost_usd": 0.25},
                    },
                )
                self.assertTrue(storage.record_event(event))
                run = storage.get_run(event["run_id"])
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(run["agent"], "codex")
                self.assertEqual(run["tool_calls"], 1)
                self.assertEqual(run["total_cost_usd_est"], 0.25)
                self.assertTrue(run["checks_ran"])
            finally:
                storage.close()

    def test_list_runs_filters_by_agent_repo_branch_status_and_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                codex = normalize_event(
                    "user-prompt-submit",
                    {
                        "agent": "codex",
                        "session_id": "filters-codex",
                        "repo": "api",
                        "branch": "main",
                        "prompt": "codex",
                        "labels": {"task": "auth", "user": "daniel"},
                    },
                )
                claude = normalize_event(
                    "user-prompt-submit",
                    {
                        "agent": "claude-code",
                        "session_id": "filters-claude",
                        "repo": "web",
                        "branch": "feature",
                        "prompt": "claude",
                        "labels": {"task": "billing"},
                    },
                )
                claude_done = normalize_event(
                    "session-end",
                    {
                        "agent": "claude-code",
                        "session_id": "filters-claude",
                        "repo": "web",
                        "branch": "feature",
                        "labels": {"kind": "scheduled"},
                    },
                )
                for event in (codex, claude, claude_done):
                    storage.record_event(event)
                self.assertEqual([run["run_id"] for run in storage.list_runs(agent="codex")], [codex["run_id"]])
                self.assertEqual([run["run_id"] for run in storage.list_runs(repo="web", branch="feature")], [claude["run_id"]])
                self.assertEqual([run["run_id"] for run in storage.list_runs(status="completed")], [claude["run_id"]])
                self.assertEqual([run["run_id"] for run in storage.list_runs(labels="task=auth")], [codex["run_id"]])
                self.assertEqual([run["run_id"] for run in storage.list_runs(labels=["task=billing", "kind"])], [claude["run_id"]])
                self.assertEqual(storage.list_runs(agent="codex", repo="web"), [])
                self.assertEqual(storage.get_run(codex["run_id"])["labels"]["task"], ["auth"])
            finally:
                storage.close()

    def test_hook_transcript_duplicate_prefers_hook_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                base = {
                    "session_id": "reconcile",
                    "timestamp": "2026-06-17T12:00:00Z",
                    "repo": "repo",
                    "branch": "main",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pytest -q"},
                }
                transcript_event = normalize_event("post-tool-use", dict(base), source="transcript")
                hook_event = normalize_event(
                    "post-tool-use",
                    {
                        **base,
                        "tool_output": {"summary": "ok"},
                        "usage": {"cost_usd": 0.25},
                        "exit_code": 0,
                    },
                    source="hook",
                )
                self.assertTrue(storage.record_event(transcript_event))
                self.assertTrue(storage.record_event(hook_event))
                self.assertFalse(storage.record_event(normalize_event("post-tool-use", dict(base), source="transcript")))
                events = storage.get_run_events(transcript_event["run_id"])
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["source"], "hook")
                self.assertEqual(events[0]["event_id"], hook_event["event_id"])
                self.assertEqual(events[0]["tool"]["output"]["exit_code"], 0)
                run = storage.get_run(transcript_event["run_id"])
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(run["tool_calls"], 1)
                self.assertEqual(run["total_cost_usd_est"], 0.25)
            finally:
                storage.close()

    def test_same_source_repeated_tools_are_not_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                payload = {
                    "session_id": "same-source-repeat",
                    "timestamp": "2026-06-17T12:00:00Z",
                    "tool_name": "Bash",
                    "tool_input": {"command": "npm test"},
                }
                first = normalize_event("post-tool-use", dict(payload), source="hook")
                second = normalize_event("post-tool-use", dict(payload), source="hook")
                self.assertTrue(storage.record_event(first))
                self.assertTrue(storage.record_event(second))
                events = storage.get_run_events(first["run_id"])
                self.assertEqual(len(events), 2)
                run = storage.get_run(first["run_id"])
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(run["tool_calls"], 2)
            finally:
                storage.close()

    def test_subagent_sessions_roll_up_to_parent_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                parent = normalize_event(
                    "user-prompt-submit",
                    {"session_id": "parent-session", "repo": "repo", "branch": "main", "prompt": "delegate"},
                )
                child_tool = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "child-session",
                        "parent_session_id": "parent-session",
                        "depth": 1,
                        "repo": "repo",
                        "branch": "main",
                        "model": "claude-sonnet",
                        "resolved_model": "claude-opus",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest"},
                        "usage": {"cost_usd": 0.4},
                    },
                )
                child_done = normalize_event(
                    "subagent-stop",
                    {
                        "session_id": "child-session",
                        "parent_session_id": "parent-session",
                        "depth": 1,
                        "repo": "repo",
                        "branch": "main",
                        "message": "subtask complete",
                    },
                )
                for event in (parent, child_tool, child_done):
                    storage.record_event(event)
                self.assertEqual(parent["run_id"], child_tool["run_id"])
                run = storage.get_run(parent["run_id"])
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(run["subagents_count"], 1)
                self.assertEqual(run["max_depth"], 1)
                subagents = storage.list_subagents(parent["run_id"])
                self.assertEqual(len(subagents), 1)
                self.assertEqual(subagents[0]["session_id"], "child-session")
                self.assertEqual(subagents[0]["parent_session_id"], "parent-session")
                self.assertEqual(subagents[0]["model"], "claude-opus")
                self.assertEqual(subagents[0]["status"], "completed")
                self.assertEqual(subagents[0]["cost_usd_est"], 0.4)
            finally:
                storage.close()

    def test_file_touch_summary_counts_reads_writes_and_thrash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from tranquil.config import SignalThresholds

            storage = Storage(Path(tmp) / "tranquil.db", thresholds=SignalThresholds(reread_repeats=2))
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "files", "prompt": "inspect file"})
                read_one = normalize_event(
                    "post-tool-use",
                    {"session_id": "files", "tool_name": "Read", "tool_input": {"file_path": "app.py"}},
                )
                read_two = normalize_event(
                    "post-tool-use",
                    {"session_id": "files", "tool_name": "Read", "tool_input": {"file_path": "app.py"}},
                )
                write = normalize_event(
                    "post-tool-use",
                    {"session_id": "files", "tool_name": "Write", "tool_input": {"file_path": "app.py", "content": "print('ok')"}},
                )
                other = normalize_event(
                    "file-changed",
                    {"session_id": "files", "file_path": "README.md"},
                )
                for event in (prompt, read_one, read_two, write, other):
                    storage.record_event(event)
                summary = {item["path"]: item for item in storage.file_touch_summary(prompt["run_id"])}
                self.assertEqual(summary["app.py"]["reads"], 2)
                self.assertEqual(summary["app.py"]["writes"], 1)
                self.assertTrue(summary["app.py"]["reread_thrash"])
                self.assertEqual(summary["README.md"]["writes"], 1)
                self.assertEqual(summary["README.md"]["reads"], 0)
            finally:
                storage.close()

    def test_run_display_events_include_inline_diffs_for_edit_and_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                write = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "diffs",
                        "tool_name": "Write",
                        "tool_input": {"file_path": "app.py", "content": "print('new')\n"},
                    },
                )
                edit = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "diffs",
                        "tool_name": "Edit",
                        "tool_input": {"file_path": "app.py", "old_string": "old = 1\n", "new_string": "new = 2\n"},
                    },
                )
                for event in (write, edit):
                    storage.record_event(event)
                raw_events = storage.get_run_events(write["run_id"])
                self.assertNotIn("diff", raw_events[0])
                display_events = storage.get_run_display_events(write["run_id"])
                diffs = [event["diff"] for event in display_events]
                self.assertEqual(diffs[0]["kind"], "write")
                self.assertIn("+print('new')", diffs[0]["text"])
                self.assertEqual(diffs[1]["kind"], "edit")
                self.assertIn("-old = 1", diffs[1]["text"])
                self.assertIn("+new = 2", diffs[1]["text"])
            finally:
                storage.close()

    def test_tui_renderers_show_fleet_and_run_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                prompt = normalize_event(
                    "user-prompt-submit",
                    {
                        "session_id": "tui",
                        "repo": "api",
                        "branch": "main",
                        "prompt": "inspect auth",
                        "labels": {"task": "auth"},
                    },
                )
                read = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "tui",
                        "repo": "api",
                        "branch": "main",
                        "tool_name": "Read",
                        "tool_input": {"file_path": "app.py"},
                        "usage": {"cost_usd": 0.1},
                    },
                )
                end = normalize_event("session-end", {"session_id": "tui", "repo": "api", "branch": "main"})
                for event in (prompt, read, end):
                    storage.record_event(event)
                fleet = render_fleet(storage, SignalThresholds(), limit=5)
                self.assertIn("TRANQUIL Fleet", fleet)
                self.assertIn("api / main", fleet)
                self.assertIn("task=auth", fleet)
                self.assertIn("Peek", fleet)
                self.assertIn("Active Signals", fleet)
                self.assertIn("Recent Evals", fleet)
                self.assertIn("Keys:", fleet)
                run = render_run(storage, SignalThresholds(), prompt["run_id"])
                self.assertIn("TRANQUIL Run", run)
                self.assertIn("Context", run)
                self.assertIn("Prompt: inspect auth", run)
                self.assertIn("app.py", run)
                self.assertIn("Recent Events", run)
                self.assertIn("Eval Scores", run)
                self.assertIn("Keys:", run)
            finally:
                storage.close()

    def test_tui_cli_once_renders_run_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from tranquil.config import save_config

            root = Path(tmp)
            home = root / "home"
            config = TranquilConfig(home=home, db_path=root / "tranquil.db")
            save_config(config)
            storage = Storage(config.db_path)
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "tui-cli", "prompt": "show run"})
                storage.record_event(prompt)
            finally:
                storage.close()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = tranquil_main(["--home", str(home), "tui", "--once", "--run", prompt["run_id"]])
            self.assertEqual(code, 0)
            output = stdout.getvalue()
            self.assertIn("TRANQUIL Run", output)
            self.assertIn("show run", output)

    def test_raw_payload_storage_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db", raw_payloads=False)
            try:
                event = normalize_event(
                    "user-prompt-submit",
                    {"session_id": "no-raw", "prompt": "hello", "api_key": "sk-secretsecretsecretsecret"},
                )
                storage.record_event(event)
                stored = storage.get_run_events(event["run_id"])[0]
                self.assertEqual(stored["message"], "hello")
                self.assertEqual(stored["raw"], {})
            finally:
                storage.close()

    def test_loop_signal_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                for index in range(3):
                    event = normalize_event(
                        "post-tool-use",
                        {
                            "event_id": f"evt_{index}",
                            "session_id": "loop",
                            "tool_name": "Bash",
                            "tool_input": {"command": "npm test"},
                        },
                    )
                    storage.record_event(event)
                signals = storage.list_signals()
                self.assertEqual([signal["type"] for signal in signals], ["loop"])
                fixtures = storage.create_fixtures_from_signals()
                self.assertEqual(len(fixtures), 1)
                self.assertEqual(fixtures[0]["suite"], "signals")
            finally:
                storage.close()

    def test_trace_sampling_creates_completed_fixtures_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "sampled-run", "prompt": "ship a fix"})
                end = normalize_event("session-end", {"session_id": "sampled-run"})
                storage.record_event(prompt)
                storage.record_event(end)
                fixtures = storage.sample_runs(suite="sampled", sample_rate=1.0, limit=10)
                self.assertEqual(len(fixtures), 1)
                self.assertEqual(fixtures[0]["run_id"], prompt["run_id"])
                self.assertEqual(fixtures[0]["suite"], "sampled")
                self.assertEqual(fixtures[0]["repo_ref"]["sampled_from"], "production_trace")
                self.assertEqual(fixtures[0]["repo_ref"]["sample_rate"], 1.0)
                self.assertEqual(storage.sample_runs(suite="sampled", sample_rate=1.0, limit=10), [])
                self.assertEqual(storage.sample_runs(suite="never", sample_rate=0.0, limit=10), [])
            finally:
                storage.close()

    def test_fixture_sample_cli_samples_completed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from tranquil.config import save_config

            root = Path(tmp)
            home = root / "home"
            config = TranquilConfig(home=home, db_path=root / "tranquil.db")
            save_config(config)
            storage = Storage(config.db_path)
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "sample-cli", "prompt": "capture me"})
                end = normalize_event("session-end", {"session_id": "sample-cli"})
                storage.record_event(prompt)
                storage.record_event(end)
            finally:
                storage.close()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = tranquil_main(["--home", str(home), "fixture", "sample", "--suite", "sampled", "--rate", "1.0"])
            self.assertEqual(code, 0)
            self.assertIn("fixture:", stdout.getvalue())
            storage = Storage(config.db_path)
            try:
                self.assertEqual(len(storage.list_fixtures(suite="sampled")), 1)
            finally:
                storage.close()

    def test_sync_cli_pushes_local_export_to_opt_in_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from tranquil.config import save_config

            received: list[dict[str, object]] = []
            headers: list[str | None] = []

            class Receiver(BaseHTTPRequestHandler):
                def log_message(self, format: str, *args: object) -> None:
                    return

                def do_POST(self) -> None:  # noqa: N802
                    length = int(self.headers.get("Content-Length") or "0")
                    headers.append(self.headers.get("X-Tranquil-Test"))
                    received.append(json.loads(self.rfile.read(length).decode("utf-8")))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")

            receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
            thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            thread.start()
            root = Path(tmp)
            home = root / "home"
            config = TranquilConfig(home=home, db_path=root / "tranquil.db")
            save_config(config)
            storage = Storage(config.db_path)
            try:
                event = normalize_event("user-prompt-submit", {"session_id": "sync", "prompt": "sync me"})
                storage.record_event(event)
            finally:
                storage.close()
            try:
                stdout = io.StringIO()
                endpoint = f"http://127.0.0.1:{receiver.server_address[1]}/sync"
                with contextlib.redirect_stdout(stdout):
                    code = tranquil_main(
                        ["--home", str(home), "sync", "--endpoint", endpoint, "--header", "X-Tranquil-Test=yes"]
                    )
                self.assertEqual(code, 0)
                self.assertIn("sync: status=200", stdout.getvalue())
                self.assertEqual(headers, ["yes"])
                self.assertEqual(received[0]["schema"], "tranquil.sync/v1")
                data = received[0]["data"]
                assert isinstance(data, dict)
                self.assertEqual(len(data["runs"]), 1)
            finally:
                receiver.shutdown()
                receiver.server_close()

    def test_doctor_codex_audit_reports_rollout_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from tranquil.config import save_config

            root = Path(tmp)
            home = root / "home"
            rollout = root / "codex.db"
            conn = sqlite3.connect(rollout)
            try:
                conn.execute("CREATE TABLE items (payload TEXT)")
                conn.execute(
                    "INSERT INTO items (payload) VALUES (?)",
                    (
                        json.dumps(
                            {
                                "session_id": "cx",
                                "timestamp": "2026-06-17T12:00:00Z",
                                "tool_name": "Bash",
                                "tool_input": {"command": "pytest"},
                                "usage": {"input_tokens": 1, "output_tokens": 2, "cost_usd": 0.01},
                            }
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            config = TranquilConfig(home=home, db_path=root / "tranquil.db")
            config.codex_rollout_paths = [str(rollout)]
            save_config(config)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = tranquil_main(["--home", str(home), "doctor", "--codex-audit"])
            self.assertEqual(code, 0)
            output = stdout.getvalue()
            self.assertIn("codex audit:", output)
            self.assertIn("files: 1", output)
            self.assertIn("has_tool: yes", output)
            self.assertIn("has_usage: yes", output)

    def test_signal_sink_receives_new_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delivered = []
            storage = Storage(Path(tmp) / "tranquil.db", signal_sink=delivered.append)
            try:
                for index in range(3):
                    event = normalize_event(
                        "post-tool-use",
                        {
                            "event_id": f"evt_sink_{index}",
                            "session_id": "signal-sink",
                            "tool_name": "Bash",
                            "tool_input": {"command": "npm test"},
                        },
                    )
                    storage.record_event(event)
                self.assertEqual(len(delivered), 1)
                self.assertEqual(delivered[0]["type"], "loop")
                self.assertEqual(delivered[0]["run_id"], event["run_id"])
            finally:
                storage.close()

    def test_signal_notifier_runs_optional_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from tranquil.notifications import SignalNotifier

            root = Path(tmp)
            output = root / "notification.json"
            config = TranquilConfig(home=root, db_path=root / "tranquil.db")
            code = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')"
            config.notification_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} {shlex.quote(str(output))}"
            notifier = SignalNotifier(config)
            notifier.notify_signal({"signal_id": "sig_test", "run_id": "run_test", "type": "loop", "severity": "high"})
            for _index in range(50):
                if output.exists():
                    break
                time.sleep(0.05)
            self.assertTrue(output.exists())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["type"], "signal")
            self.assertEqual(payload["signal"]["type"], "loop")

    def test_scheduled_idle_signal_uses_scheduled_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from tranquil.config import SignalThresholds
            from tranquil.signals import scan_idle_runs

            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                event = normalize_event(
                    "user-prompt-submit",
                    {
                        "session_id": "scheduled-idle",
                        "timestamp": time.time() - 120 * 60,
                        "prompt": "nightly dependency update",
                        "labels": {"kind": "scheduled"},
                    },
                )
                storage.record_event(event)
                scan_idle_runs(storage, SignalThresholds(idle_minutes=999, scheduled_idle_minutes=1))
                signals = storage.list_signals()
                by_type = {signal["type"]: signal for signal in signals}
                self.assertIn("scheduled_idle", by_type)
                self.assertNotIn("stuck_idle", by_type)
                self.assertEqual(by_type["scheduled_idle"]["severity"], "high")
                self.assertEqual(by_type["scheduled_idle"]["evidence"]["threshold_minutes"], 1)
            finally:
                storage.close()

    def test_stop_cli_records_request_and_pre_tool_denies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from tranquil.config import save_config

            root = Path(tmp)
            home = root / "home"
            config = TranquilConfig(home=home, db_path=root / "tranquil.db")
            save_config(config)
            storage = Storage(config.db_path)
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "stop-me", "prompt": "run forever"})
                storage.record_event(prompt)
            finally:
                storage.close()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = tranquil_main(["--home", str(home), "stop", prompt["run_id"]])
            self.assertEqual(code, 0)
            self.assertIn("stop requested", stdout.getvalue())
            storage = Storage(config.db_path)
            try:
                signals = storage.list_run_signals(prompt["run_id"])
                self.assertEqual([signal["type"] for signal in signals], ["stop_requested"])
                pre_tool = normalize_event("pre-tool-use", {"session_id": "stop-me", "tool_name": "Bash"})
                decision = pre_tool_decision(storage, config, pre_tool)
                self.assertIn("stop requested", decision or "")
            finally:
                storage.close()

    def test_skipped_checks_signal_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                commit = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "ship",
                        "tool_name": "Bash",
                        "tool_input": {"command": "git commit -am ship"},
                    },
                )
                end = normalize_event("session-end", {"session_id": "ship"})
                storage.record_event(commit)
                storage.record_event(end)
                signals = storage.list_signals()
                self.assertIn("skipped_checks", {signal["type"] for signal in signals})
            finally:
                storage.close()

    def test_fixture_and_eval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "fixture", "prompt": "fix auth"})
                check = normalize_event(
                    "post-tool-use",
                    {"session_id": "fixture", "tool_name": "Bash", "tool_input": {"command": "pytest"}},
                )
                end = normalize_event("session-end", {"session_id": "fixture"})
                for event in (prompt, check, end):
                    storage.record_event(event)
                fixture = storage.create_fixture(prompt["run_id"], suite="smoke")
                self.assertEqual(fixture["prompt"], "fix auth")
                eval_run_id, scores = run_eval(storage, suite="smoke")
                self.assertTrue(eval_run_id.startswith("eval_"))
                self.assertTrue(scores)
                self.assertTrue(all("scorer" in score for score in scores))
            finally:
                storage.close()

    def test_eval_design_scorers_use_command_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "scorers", "prompt": "make tests pass"})
                patch = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "scorers",
                        "tool_name": "apply_patch",
                        "tool_input": {"patch": "*** Begin Patch\n*** End Patch"},
                        "exit_code": 0,
                    },
                )
                test = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "scorers",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest -q"},
                        "exit_code": 0,
                    },
                )
                build = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "scorers",
                        "tool_name": "Bash",
                        "tool_input": {"command": "npm run build"},
                        "tool_output": {"summary": "compiled"},
                        "exit_code": 0,
                    },
                )
                end = normalize_event("session-end", {"session_id": "scorers"})
                for event in (prompt, patch, test, build, end):
                    storage.record_event(event)
                storage.create_fixture(prompt["run_id"], suite="scorers")
                _eval_run_id, scores = run_eval(storage, suite="scorers")
                by_scorer = {score["scorer"]: score for score in scores}
                for scorer in ("tests_pass", "build_succeeds", "diff_applies", "no_loops"):
                    self.assertIn(scorer, by_scorer)
                    self.assertTrue(by_scorer[scorer]["passed"])
                self.assertEqual(by_scorer["tests_pass"]["value"], 1.0)
                self.assertEqual(by_scorer["build_succeeds"]["value"], 1.0)
                self.assertEqual(by_scorer["diff_applies"]["value"], 1.0)
                self.assertEqual(by_scorer["tests_pass"]["detail"]["commands"][0]["exit_code"], 0)
            finally:
                storage.close()

    def test_eval_tests_pass_fails_on_nonzero_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "failing-tests", "prompt": "fix failing test"})
                test = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "failing-tests",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest -q"},
                        "exit_code": 1,
                    },
                )
                end = normalize_event("session-end", {"session_id": "failing-tests"})
                for event in (prompt, test, end):
                    storage.record_event(event)
                storage.create_fixture(prompt["run_id"], suite="failing-tests")
                eval_run_id, scores = run_eval(storage, suite="failing-tests")
                by_scorer = {score["scorer"]: score for score in scores}
                self.assertFalse(by_scorer["tests_pass"]["passed"])
                self.assertEqual(by_scorer["tests_pass"]["value"], 0.0)
                self.assertEqual(by_scorer["tests_pass"]["detail"]["failures"][0]["exit_code"], 1)
                latest = storage.latest_eval_run("failing-tests")
                self.assertIsNotNone(latest)
                assert latest is not None
                self.assertEqual(latest["eval_run_id"], eval_run_id)
                self.assertEqual(latest["status"], "failed")
            finally:
                storage.close()

    def test_run_scores_are_linked_back_through_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "run-score", "prompt": "test it"})
                test = normalize_event(
                    "post-tool-use",
                    {"session_id": "run-score", "tool_name": "Bash", "tool_input": {"command": "pytest -q"}, "exit_code": 0},
                )
                for event in (prompt, test):
                    storage.record_event(event)
                storage.create_fixture(prompt["run_id"], suite="run-score")
                eval_run_id, _scores = run_eval(storage, suite="run-score", scorers=["tests_pass"])
                run_scores = storage.list_run_scores(prompt["run_id"])
                self.assertEqual(len(run_scores), 1)
                self.assertEqual(run_scores[0]["eval_run_id"], eval_run_id)
                self.assertEqual(run_scores[0]["scorer"], "tests_pass")
                self.assertEqual(run_scores[0]["suite"], "run-score")
                self.assertTrue(run_scores[0]["passed"])
            finally:
                storage.close()

    def test_fixture_and_suite_yaml_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = Storage(root / "tranquil.db")
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "yaml", "prompt": "refactor auth"})
                test = normalize_event(
                    "post-tool-use",
                    {"session_id": "yaml", "tool_name": "Bash", "tool_input": {"command": "pytest"}, "usage": {"cost_usd": 0.2}},
                )
                end = normalize_event("session-end", {"session_id": "yaml"})
                for event in (prompt, test, end):
                    storage.record_event(event)
                fixture_dir = root / "tranquil" / "fixtures"
                suite_dir = root / "tranquil" / "suites"
                fixture_dir.mkdir(parents=True)
                suite_dir.mkdir(parents=True)
                fixture_path = fixture_dir / "refactor-auth.yaml"
                fixture_path.write_text(
                    "\n".join(
                        [
                            "fixture: refactor-auth",
                            f"from_run: {prompt['run_id']}",
                            'prompt: "Refactor auth safely."',
                            "repo_ref: { repo: acme/api, sha: abc123 }",
                            "budgets: { cost_usd: 1.50, wall_clock_s: 900 }",
                            'forbidden_paths: [".env", "secrets/**"]',
                            'rubric: "Keep auth behavior safe."',
                            'reference: "All tests stay green."',
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                suite_path = suite_dir / "refactor.yaml"
                suite_path.write_text(
                    "\n".join(
                        [
                            "suite: refactor",
                            "baseline: last-green",
                            "matrix:",
                            "  - { model: claude-opus-4-8 }",
                            "  - { model: claude-sonnet-4-6 }",
                            "fixtures: [refactor-auth]",
                            "scorers: [tests_pass, build_succeeds, no_forbidden_paths]",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                fixture = import_fixture_file(storage, fixture_path)
                self.assertEqual(fixture["fixture_id"], "refactor-auth")
                self.assertEqual(fixture["repo_ref"]["budgets"]["cost_usd"], 1.5)
                self.assertEqual(fixture["repo_ref"]["rubric"], "Keep auth behavior safe.")
                self.assertEqual(fixture["repo_ref"]["reference"], "All tests stay green.")
                suite = load_suite_file(suite_path)
                self.assertEqual(len(suite["matrix"]), 2)
                self.assertEqual(suite["scorers"], ["tests_pass", "build_succeeds", "no_forbidden_paths"])
                suite_def, imported = import_suite_fixtures(storage, suite_path)
                self.assertEqual(suite_def["suite"], "refactor")
                self.assertEqual(len(imported), 1)
                eval_run_id, scores = run_eval(
                    storage,
                    suite="refactor",
                    baseline=suite_def["baseline"],
                    scorers=suite_def["scorers"],
                )
                self.assertTrue(eval_run_id.startswith("eval_"))
                self.assertTrue(all(score["passed"] for score in scores))
                self.assertEqual({score["scorer"] for score in scores}, {"tests_pass", "build_succeeds", "no_forbidden_paths"})
                matrix_results = run_eval_matrix(
                    storage,
                    suite="refactor",
                    matrix=[{"name": "prompt-file", "command": 'test -f "$TRANQUIL_PROMPT_FILE"'}],
                    replay_root=root / "replays",
                )
                self.assertEqual(len(matrix_results), 1)
                self.assertEqual(matrix_results[0]["variant"], "prompt-file")
                matrix_scores = {score["scorer"]: score for score in matrix_results[0]["scores"]}
                self.assertTrue(matrix_scores["matrix_variant"]["passed"])
                self.assertTrue(matrix_scores["repo_materialized"]["passed"])
                self.assertTrue(matrix_scores["replay_command_exits_zero"]["passed"])
                latest_matrix = storage.latest_eval_run("refactor:prompt-file")
                self.assertIsNotNone(latest_matrix)
                assert latest_matrix is not None
                self.assertEqual(latest_matrix["status"], "passed")
            finally:
                storage.close()

    def test_eval_outcome_judge_requires_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "unconfigured-judge", "prompt": "judge me"})
                storage.record_event(prompt)
                storage.create_fixture(prompt["run_id"], suite="unconfigured-judge")
                eval_run_id, scores = run_eval(storage, suite="unconfigured-judge", scorers=["outcome_judge"])
                by_scorer = {score["scorer"]: score for score in scores}
                self.assertIn("outcome_judge", by_scorer)
                self.assertFalse(by_scorer["outcome_judge"]["passed"])
                self.assertEqual(by_scorer["outcome_judge"]["detail"]["status"], "unconfigured")
                latest = storage.latest_eval_run("unconfigured-judge")
                self.assertIsNotNone(latest)
                assert latest is not None
                self.assertEqual(latest["eval_run_id"], eval_run_id)
                self.assertEqual(latest["status"], "failed")
            finally:
                storage.close()

    def test_eval_outcome_judge_uses_command_and_fixture_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "judge", "prompt": "ship"})
                storage.record_event(prompt)
                storage.upsert_fixture_definition(
                    "judge-fixture",
                    prompt["run_id"],
                    suite="judge",
                    rubric="safe",
                    reference="expected",
                )
                code = (
                    "import json, sys; "
                    "data = json.load(sys.stdin); "
                    "passed = data.get('rubric') == 'safe' and data.get('reference') == 'expected'; "
                    "print(json.dumps({'passed': passed, 'score': 0.9, 'reason': 'metadata ok'}))"
                )
                judge_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
                eval_run_id, scores = run_eval(storage, suite="judge", scorers=["outcome_judge"], judge_command=judge_command)
                by_scorer = {score["scorer"]: score for score in scores}
                self.assertIn("outcome_judge", by_scorer)
                self.assertTrue(by_scorer["outcome_judge"]["passed"])
                self.assertEqual(by_scorer["outcome_judge"]["value"], 0.9)
                self.assertEqual(by_scorer["outcome_judge"]["detail"]["reason"], "metadata ok")
                latest = storage.latest_eval_run("judge")
                self.assertIsNotNone(latest)
                assert latest is not None
                self.assertEqual(latest["eval_run_id"], eval_run_id)
                self.assertEqual(latest["status"], "passed")
            finally:
                storage.close()

    def test_eval_scores_budgets_forbidden_paths_and_regressions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "budget", "prompt": "ship safely"})
                write = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "budget",
                        "tool_name": "Write",
                        "tool_input": {"file_path": ".env", "content": "SECRET=value"},
                        "usage": {"cost_usd": 0.5},
                    },
                )
                end = normalize_event("session-end", {"session_id": "budget"})
                for event in (prompt, write, end):
                    storage.record_event(event)
                fixture = storage.create_fixture(
                    prompt["run_id"],
                    suite="budget",
                    cost_budget_usd=1.0,
                    latency_budget_s=3600,
                    forbidden_paths=["secrets/**"],
                )
                first_eval_run_id, first_scores = run_eval(storage, suite="budget")
                self.assertTrue(first_scores)
                self.assertTrue(all(score["passed"] for score in first_scores))
                repo_ref = fixture["repo_ref"]
                repo_ref["budgets"]["cost_usd"] = 0.1
                repo_ref["forbidden_paths"] = [".env"]
                storage._conn.execute(
                    "UPDATE fixtures SET repo_ref_json = ? WHERE fixture_id = ?",
                    (json.dumps(repo_ref, sort_keys=True), fixture["fixture_id"]),
                )
                storage._conn.commit()
                second_eval_run_id, second_scores = run_eval(storage, suite="budget", baseline="last-green")
                self.assertNotEqual(first_eval_run_id, second_eval_run_id)
                by_scorer = {score["scorer"]: score for score in second_scores}
                self.assertFalse(by_scorer["cost_budget"]["passed"])
                self.assertFalse(by_scorer["no_forbidden_paths"]["passed"])
                self.assertFalse(by_scorer["regression"]["passed"])
            finally:
                storage.close()

    def test_replay_materializes_git_worktree(self) -> None:
        if not shutil.which("git"):
            self.skipTest("git is required")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "tranquil@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tranquil"], check=True)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (repo / "README.md").write_text("hello\nchanged\n", encoding="utf-8")
            storage = Storage(root / "tranquil.db")
            try:
                event = normalize_event("user-prompt-submit", {"session_id": "git-replay", "prompt": "inspect readme", "cwd": str(repo)})
                storage.record_event(event)
                fixture = storage.create_fixture(event["run_id"], suite="git")
                self.assertIn("sha", fixture["repo_ref"])
                self.assertTrue(fixture["repo_ref"]["dirty"])
                self.assertIn("dirty_patch", fixture["repo_ref"])
                eval_run_id, scores = replay_fixture(storage, fixture["fixture_id"], "grep changed README.md", root / "replays")
                self.assertTrue(eval_run_id.startswith("eval_"))
                by_scorer = {score["scorer"]: score for score in scores}
                self.assertTrue(by_scorer["repo_materialized"]["passed"])
                self.assertTrue(by_scorer["repo_materialized"]["detail"]["dirty_patch_applied"])
                self.assertTrue(by_scorer["replay_command_exits_zero"]["passed"])
            finally:
                storage.close()

    def test_export_and_purge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                event = normalize_event("user-prompt-submit", {"session_id": "purge", "prompt": "clean"})
                storage.record_event(event)
                exported = storage.export_data()
                self.assertEqual(len(exported["runs"]), 1)
                counts = storage.purge(all_data=True)
                self.assertEqual(counts["runs"], 1)
                self.assertEqual(storage.list_runs(), [])
            finally:
                storage.close()

    def test_retention_purge_preserves_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                event = normalize_event("user-prompt-submit", {"session_id": "keep-fixture", "prompt": "preserve"})
                storage.record_event(event)
                fixture = storage.create_fixture(event["run_id"])
                old = "2000-01-01T00:00:00.000Z"
                storage._conn.execute("UPDATE runs SET last_event_at = ?, started_at = ? WHERE run_id = ?", (old, old, event["run_id"]))
                storage._conn.commit()
                counts = storage.purge(older_than_days=1)
                self.assertGreaterEqual(counts["events"], 1)
                self.assertEqual(counts["fixtures"], 0)
                self.assertIsNotNone(storage.get_fixture(fixture["fixture_id"]))
                self.assertIsNotNone(storage.get_run(event["run_id"]))
            finally:
                storage.close()

    def test_otlp_payload_contains_event_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                event = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "otel",
                        "agent": "codex",
                        "repo": "repo",
                        "branch": "main",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest"},
                        "usage": {"cost_usd": 0.1},
                    },
                )
                storage.record_event(event)
                payload = build_otlp_logs_payload(storage)
                records = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
                self.assertEqual(len(records), 1)
                attrs = {item["key"]: item["value"] for item in records[0]["attributes"]}
                self.assertEqual(attrs["tranquil.agent"]["stringValue"], "codex")
                self.assertEqual(attrs["tranquil.cost_usd_est"]["doubleValue"], 0.1)
            finally:
                storage.close()


class IngestTests(unittest.TestCase):
    def test_jsonl_ingest_backfills_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            transcript.write_text(
                json.dumps({"type": "user", "session_id": "jsonl", "prompt": "build it"}) + "\n"
                + json.dumps({"session_id": "jsonl", "tool_name": "Bash", "tool_input": {"command": "pytest"}}) + "\n",
                encoding="utf-8",
            )
            storage = Storage(root / "tranquil.db")
            try:
                count = ingest_path(storage, transcript)
                self.assertEqual(count, 2)
                runs = storage.list_runs()
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["first_prompt"], "build it")
                self.assertTrue(runs[0]["checks_ran"])
            finally:
                storage.close()

    def test_rollout_tailer_imports_new_sqlite_rows_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / "codex.db"
            conn = sqlite3.connect(rollout)
            try:
                conn.execute("CREATE TABLE rollouts (payload TEXT)")
                conn.execute(
                    "INSERT INTO rollouts (payload) VALUES (?)",
                    (
                        json.dumps(
                            {
                                "agent": "codex",
                                "session_id": "rollout-tail",
                                "timestamp": "2026-06-17T12:00:00Z",
                                "prompt": "tail me",
                            }
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            storage = Storage(root / "tranquil.db")
            try:
                tailer = RolloutTailer(storage, [str(rollout)])
                self.assertEqual(tailer.scan_once(), 1)
                self.assertEqual(tailer.scan_once(), 0)
                conn = sqlite3.connect(rollout)
                try:
                    conn.execute(
                        "INSERT INTO rollouts (payload) VALUES (?)",
                        (
                            json.dumps(
                                {
                                    "agent": "codex",
                                    "session_id": "rollout-tail",
                                    "timestamp": "2026-06-17T12:01:00Z",
                                    "tool_name": "Bash",
                                    "tool_input": {"command": "pytest"},
                                }
                            ),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
                self.assertEqual(tailer.scan_once(), 1)
                events = storage.get_run_events(normalize_event("user-prompt-submit", {"agent": "codex", "session_id": "rollout-tail"})["run_id"])
                self.assertEqual(len(events), 2)
                self.assertTrue(storage.get_run(events[0]["run_id"])["checks_ran"])
            finally:
                storage.close()


class McpTests(unittest.TestCase):
    def test_mcp_lists_and_calls_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                event = normalize_event("user-prompt-submit", {"session_id": "mcp", "prompt": "status"})
                storage.record_event(event)
                stdin = io.StringIO(
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
                    + json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "tranquil_query_runs", "arguments": {"limit": 5}},
                        }
                    )
                    + "\n"
                )
                stdout = io.StringIO()
                run_mcp_server(storage, stdin=stdin, stdout=stdout)
                lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
                self.assertEqual(lines[0]["id"], 1)
                self.assertTrue(lines[0]["result"]["tools"])
                self.assertEqual(lines[1]["id"], 2)
                self.assertIn("run_", lines[1]["result"]["content"][0]["text"])
            finally:
                storage.close()

    def test_mcp_query_runs_since_and_cost_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "tranquil.db")
            try:
                old = normalize_event(
                    "post-tool-use",
                    {
                        "agent": "claude-code",
                        "session_id": "mcp-old",
                        "timestamp": "2026-06-16T12:00:00Z",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest"},
                        "usage": {"cost_usd": 0.1},
                    },
                )
                new = normalize_event(
                    "post-tool-use",
                    {
                        "agent": "codex",
                        "session_id": "mcp-new",
                        "timestamp": "2026-06-17T12:00:00Z",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest"},
                        "usage": {"cost_usd": 0.5},
                    },
                )
                storage.record_event(old)
                storage.record_event(new)
                stdin = io.StringIO(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "tranquil_query_runs",
                                "arguments": {"since": "2026-06-17T00:00:00.000Z", "limit": 10},
                            },
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "tranquil_cost",
                                "arguments": {"since": "2026-06-17T00:00:00.000Z", "group_by": "agent"},
                            },
                        }
                    )
                    + "\n"
                )
                stdout = io.StringIO()
                run_mcp_server(storage, stdin=stdin, stdout=stdout)
                lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
                runs = json.loads(lines[0]["result"]["content"][0]["text"])
                self.assertEqual([run["run_id"] for run in runs], [new["run_id"]])
                costs = json.loads(lines[1]["result"]["content"][0]["text"])
                self.assertEqual(costs, [{"cost_usd_est": 0.5, "key": "codex", "runs": 1}])
            finally:
                storage.close()


class InitTests(unittest.TestCase):
    def test_init_launch_decision_defaults_to_interactive_stdout(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(should_launch_after_init(SimpleNamespace(undo=False, no_launch=False, launch=False)))
            self.assertTrue(should_launch_after_init(SimpleNamespace(undo=False, no_launch=False, launch=True)))
            self.assertFalse(should_launch_after_init(SimpleNamespace(undo=False, no_launch=True, launch=True)))
            self.assertFalse(should_launch_after_init(SimpleNamespace(undo=True, no_launch=False, launch=True)))

    def test_default_command_is_terminal_app(self) -> None:
        args = build_parser().parse_args([])
        if args.command_name is None:
            args.command_name = "app"
        self.assertEqual(args.command_name, "app")

    def test_init_launch_uses_terminal_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            with mock.patch("tranquil.cli.run_terminal_app", return_value=0) as launched:
                stdout = io.StringIO()
                with contextlib.chdir(root), contextlib.redirect_stdout(stdout):
                    code = tranquil_main(["--home", str(home), "init", "--agent", "claude-code", "--scope", "project", "--launch"])
            self.assertEqual(code, 0)
            self.assertEqual(launched.call_count, 1)
            self.assertIn("launching Tranquil terminal app", stdout.getvalue())

    def test_claude_init_is_idempotent_and_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "tranquil-home"
            project = root / "project"
            project.mkdir()
            report = run_init(agent="claude-code", scope="project", home=home, cwd=project)
            self.assertTrue(report.changed)
            settings_path = project / ".claude" / "settings.json"
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertTrue(data["_tranquil"]["managed"])
            self.assertEqual(len(data["hooks"]["PostToolUse"]), 1)
            self.assertEqual(data["mcpServers"]["tranquil"]["command"], "tranquil")
            second = run_init(agent="claude-code", scope="project", home=home, cwd=project)
            self.assertIn(str(settings_path), second.unchanged)
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["hooks"]["PostToolUse"]), 1)
            undo = run_init(agent="claude-code", scope="project", home=home, cwd=project, undo=True)
            self.assertIn(str(settings_path), undo.removed)
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertNotIn("_tranquil", data)
            self.assertNotIn("hooks", data)
            self.assertNotIn("mcpServers", data)

    def test_codex_init_is_idempotent_and_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "tranquil-home"
            project = root / "project"
            project.mkdir()
            report = run_init(agent="codex", scope="project", home=home, cwd=project)
            self.assertTrue(report.changed)
            hooks_path = project / ".codex" / "hooks.json"
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertTrue(data["_tranquil"]["managed"])
            self.assertIn("PostToolUse", data["hooks"])
            command = data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
            self.assertIn("hook-forward", command)
            self.assertNotIn("Bearer", command)
            second = run_init(agent="codex", scope="project", home=home, cwd=project)
            self.assertIn(str(hooks_path), second.unchanged)
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["hooks"]["PostToolUse"]), 1)
            undo = run_init(agent="codex", scope="project", home=home, cwd=project, undo=True)
            self.assertIn(str(hooks_path), undo.removed)
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            self.assertNotIn("_tranquil", data)
            self.assertNotIn("hooks", data)


class ServerTests(unittest.TestCase):
    def test_http_hook_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = TranquilConfig(
                home=Path(tmp),
                db_path=Path(tmp) / "tranquil.db",
                host="127.0.0.1",
                port=0,
                token="test-token",
            )
            storage = Storage(config.db_path)
            server = TranquilHTTPServer(config, storage)
            server.start_worker()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/hooks/user-prompt-submit",
                    data=json.dumps({"session_id": "http", "prompt": "hello"}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Authorization": "Bearer test-token"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                server.event_queue.join()
                deadline = time.time() + 2
                run = None
                while time.time() < deadline:
                    runs = storage.list_runs()
                    if runs:
                        run = runs[0]
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(run["first_prompt"], "hello")
            finally:
                server.shutdown()
                server.stop_worker()
                server.server_close()
                storage.close()

    def test_doctor_posts_synthetic_event_when_collector_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from tranquil.config import save_config

            root = Path(tmp)
            config = TranquilConfig(
                home=root / "home",
                db_path=root / "tranquil.db",
                host="127.0.0.1",
                port=0,
                token="doctor-token",
            )
            storage = Storage(config.db_path)
            server = TranquilHTTPServer(config, storage)
            server.start_worker()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                config.port = server.server_address[1]
                save_config(config)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = tranquil_main(["--home", str(config.home), "doctor"])
                self.assertEqual(code, 0)
                self.assertIn("synthetic event: ok", stdout.getvalue())
                runs = storage.list_runs()
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["first_prompt"], "tranquil doctor synthetic event")
            finally:
                server.shutdown()
                server.stop_worker()
                server.server_close()
                storage.close()

    def test_server_trace_sampling_captures_completed_hook_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = TranquilConfig(
                home=Path(tmp),
                db_path=Path(tmp) / "tranquil.db",
                host="127.0.0.1",
                port=0,
                token="sample-token",
            )
            config.trace_sampling_enabled = True
            config.trace_sample_rate = 1.0
            config.trace_sample_suite = "sampled"
            storage = Storage(config.db_path)
            server = TranquilHTTPServer(config, storage)
            server.start_worker()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                for slug, payload in (
                    ("user-prompt-submit", {"session_id": "server-sample", "prompt": "sample this run"}),
                    ("session-end", {"session_id": "server-sample"}),
                ):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/hooks/{slug}",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json", "Authorization": "Bearer sample-token"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertEqual(response.status, 200)
                server.event_queue.join()
                fixtures = storage.list_fixtures(suite="sampled")
                self.assertEqual(len(fixtures), 1)
                self.assertEqual(fixtures[0]["prompt"], "sample this run")
            finally:
                server.shutdown()
                server.stop_worker()
                server.server_close()
                storage.close()

    def test_pre_tool_policy_denies_forbidden_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = TranquilConfig(
                home=Path(tmp),
                db_path=Path(tmp) / "tranquil.db",
                host="127.0.0.1",
                port=0,
                token="policy-token",
            )
            config.policy_enabled = True
            config.policy_forbidden_paths = [".env", "secrets/**"]
            storage = Storage(config.db_path)
            seed = normalize_event("user-prompt-submit", {"session_id": "policy", "prompt": "edit config"})
            storage.record_event(seed)
            server = TranquilHTTPServer(config, storage)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/hooks/pre-tool-use",
                    data=json.dumps({"session_id": "policy", "tool_name": "Write", "tool_input": {"file_path": ".env"}}).encode(
                        "utf-8"
                    ),
                    headers={"Content-Type": "application/json", "Authorization": "Bearer policy-token"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
                self.assertEqual(payload["permissionDecision"], "deny")
                self.assertIn(".env", payload["permissionDecisionReason"])
                signals = storage.list_run_signals(seed["run_id"])
                self.assertEqual([signal["type"] for signal in signals], ["policy_denied"])
                events = storage.get_run_events(seed["run_id"])
                self.assertEqual(events[-1]["permission"]["decision"], "deny")
            finally:
                server.shutdown()
                server.server_close()
                storage.close()

    def test_hook_forwarder_posts_codex_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = TranquilConfig(
                home=Path(tmp),
                db_path=Path(tmp) / "tranquil.db",
                host="127.0.0.1",
                port=0,
                token="forward-token",
            )
            from tranquil.config import save_config

            save_config(config)
            storage = Storage(config.db_path)
            server = TranquilHTTPServer(config, storage)
            server.start_worker()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                stdin = io.StringIO(json.dumps({"session_id": "codex-hook", "tool_name": "Bash", "tool_input": {"command": "pytest"}}))
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = hook_forward_main(
                    [
                        "--home",
                        str(config.home),
                        "--event",
                        "PostToolUse",
                        "--url",
                        f"http://127.0.0.1:{port}/hooks/post-tool-use",
                    ],
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                )
                self.assertEqual(code, 0)
                server.event_queue.join()
                runs = storage.list_runs()
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["agent"], "codex")
                self.assertTrue(runs[0]["checks_ran"])
            finally:
                server.shutdown()
                server.stop_worker()
                server.server_close()
                storage.close()

    def test_websocket_receives_event_broadcast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = TranquilConfig(
                home=Path(tmp),
                db_path=Path(tmp) / "tranquil.db",
                host="127.0.0.1",
                port=0,
                token="ws-token",
            )
            storage = Storage(config.db_path)
            server = TranquilHTTPServer(config, storage)
            server.start_worker()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            sock: socket.socket | None = None
            try:
                port = server.server_address[1]
                sock = websocket_connect("127.0.0.1", port, "/ws")
                hello = read_ws_json(sock)
                self.assertEqual(hello["type"], "hello")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/events",
                    data=json.dumps({"event_hint": "user-prompt-submit", "session_id": "ws", "prompt": "live"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                message = read_ws_json(sock)
                self.assertEqual(message["type"], "event")
                self.assertEqual(message["event_type"], "user_prompt")
                self.assertTrue(message["run_id"].startswith("run_"))
            finally:
                if sock:
                    sock.close()
                server.shutdown()
                server.stop_worker()
                server.server_close()
                storage.close()

    def test_dashboard_eval_and_run_diff_apis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = TranquilConfig(
                home=Path(tmp),
                db_path=Path(tmp) / "tranquil.db",
                host="127.0.0.1",
                port=0,
                token="api-token",
            )
            storage = Storage(config.db_path)
            server = TranquilHTTPServer(config, storage)
            server.start_worker()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                prompt_a = normalize_event("user-prompt-submit", {"session_id": "api-a", "repo": "repo", "branch": "main", "prompt": "first"})
                test_a = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "api-a",
                        "repo": "repo",
                        "branch": "main",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest -q"},
                        "exit_code": 0,
                        "usage": {"cost_usd": 0.1},
                    },
                )
                end_a = normalize_event("session-end", {"session_id": "api-a", "repo": "repo", "branch": "main"})
                prompt_b = normalize_event(
                    "user-prompt-submit",
                    {
                        "session_id": "api-b",
                        "repo": "repo",
                        "branch": "feature",
                        "prompt": "second",
                        "labels": {"task": "redesign"},
                    },
                )
                test_b = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "api-b",
                        "repo": "repo",
                        "branch": "feature",
                        "tool_name": "Bash",
                        "tool_input": {"command": "pytest -q"},
                        "exit_code": 0,
                        "usage": {"cost_usd": 0.3},
                    },
                )
                write_b = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "api-b",
                        "repo": "repo",
                        "branch": "feature",
                        "tool_name": "Write",
                        "tool_input": {"file_path": "app.py", "content": "print('ok')"},
                    },
                )
                child_b = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "api-b-child",
                        "parent_session_id": "api-b",
                        "depth": 1,
                        "repo": "repo",
                        "branch": "feature",
                        "model": "claude-sonnet",
                        "tool_name": "Read",
                        "tool_input": {"file_path": "app.py"},
                        "usage": {"cost_usd": 0.05},
                    },
                )
                end_b = normalize_event("session-end", {"session_id": "api-b", "repo": "repo", "branch": "feature"})
                for event in (prompt_a, test_a, end_a, prompt_b, test_b, write_b, child_b, end_b):
                    storage.record_event(event)
                storage.create_fixture(prompt_a["run_id"], suite="api")
                eval_run_id, _scores = run_eval(storage, suite="api", scorers=["tests_pass"])

                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/evals", timeout=2) as response:
                    evals = json.loads(response.read().decode("utf-8"))
                self.assertEqual(evals["eval_runs"][0]["eval_run_id"], eval_run_id)
                self.assertEqual(evals["eval_runs"][0]["passed_count"], 1)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/evals/{eval_run_id}", timeout=2) as response:
                    eval_detail = json.loads(response.read().decode("utf-8"))
                self.assertEqual(eval_detail["eval_run"]["score_count"], 1)
                self.assertEqual(eval_detail["scores"][0]["scorer"], "tests_pass")
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs?limit=10", timeout=2) as response:
                    runs = json.loads(response.read().decode("utf-8"))
                by_run = {run["run_id"]: run for run in runs["runs"]}
                self.assertEqual(by_run[prompt_b["run_id"]]["subagents_count"], 1)
                self.assertEqual(by_run[prompt_b["run_id"]]["max_depth"], 1)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs?repo=repo&branch=feature", timeout=2) as response:
                    filtered = json.loads(response.read().decode("utf-8"))
                self.assertEqual([run["run_id"] for run in filtered["runs"]], [prompt_b["run_id"]])
                self.assertEqual(filtered["runs"][0]["labels"]["task"], ["redesign"])
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs?label=task%3Dredesign", timeout=2) as response:
                    label_filtered = json.loads(response.read().decode("utf-8"))
                self.assertEqual([run["run_id"] for run in label_filtered["runs"]], [prompt_b["run_id"]])
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs?agent=codex", timeout=2) as response:
                    no_codex = json.loads(response.read().decode("utf-8"))
                self.assertEqual(no_codex["runs"], [])
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs/{prompt_b['run_id']}", timeout=2) as response:
                    run_detail = json.loads(response.read().decode("utf-8"))
                self.assertEqual(run_detail["run"]["subagents_count"], 1)
                self.assertEqual(run_detail["subagents"][0]["session_id"], "api-b-child")
                self.assertEqual(run_detail["subagents"][0]["model"], "claude-sonnet")
                files = {item["path"]: item for item in run_detail["files"]}
                self.assertEqual(files["app.py"]["reads"], 1)
                self.assertEqual(files["app.py"]["writes"], 1)
                diff_events = [event for event in run_detail["events"] if event.get("diff")]
                self.assertEqual(diff_events[0]["diff"]["kind"], "write")
                self.assertIn("+print('ok')", diff_events[0]["diff"]["text"])
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs/{prompt_a['run_id']}", timeout=2) as response:
                    scored_run_detail = json.loads(response.read().decode("utf-8"))
                self.assertEqual(scored_run_detail["scores"][0]["eval_run_id"], eval_run_id)
                self.assertEqual(scored_run_detail["scores"][0]["scorer"], "tests_pass")
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/runs/{prompt_a['run_id']}/diff/{prompt_b['run_id']}",
                    timeout=2,
                ) as response:
                    diff = json.loads(response.read().decode("utf-8"))
                self.assertEqual(diff["diff"]["delta"]["tool_calls"], 2)
                self.assertGreater(diff["diff"]["delta"]["cost_usd_est"], 0)
            finally:
                server.shutdown()
                server.stop_worker()
                server.server_close()
                storage.close()

    def test_run_replay_api_requires_command_and_records_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = TranquilConfig(
                home=Path(tmp),
                db_path=Path(tmp) / "tranquil.db",
                host="127.0.0.1",
                port=0,
                token="replay-token",
            )
            storage = Storage(config.db_path)
            server = TranquilHTTPServer(config, storage)
            server.start_worker()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                prompt = normalize_event("user-prompt-submit", {"session_id": "api-replay", "prompt": "replay me"})
                storage.record_event(prompt)
                port = server.server_address[1]
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/runs/{prompt['run_id']}/replay",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(raised.exception.code, 400)
                self.assertEqual(storage.list_fixtures(suite="replay"), [])

                server.config.replay_command = 'test -f "$TRANQUIL_PROMPT_FILE"'
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/runs/{prompt['run_id']}/replay",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 201)
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["fixture"]["fixture_id"].startswith("fix_"))
                self.assertTrue(payload["eval_run_id"].startswith("eval_"))
                by_scorer = {score["scorer"]: score for score in payload["scores"]}
                self.assertTrue(by_scorer["repo_materialized"]["passed"])
                self.assertTrue(by_scorer["replay_command_exits_zero"]["passed"])
            finally:
                server.shutdown()
                server.stop_worker()
                server.server_close()
                storage.close()

    def test_run_open_api_requires_editor_and_opens_touched_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("print('ok')\n", encoding="utf-8")
            marker = root / "opened.txt"
            config = TranquilConfig(
                home=root / "home",
                db_path=root / "tranquil.db",
                host="127.0.0.1",
                port=0,
                token="open-token",
            )
            storage = Storage(config.db_path)
            server = TranquilHTTPServer(config, storage)
            server.start_worker()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                event = normalize_event(
                    "post-tool-use",
                    {
                        "session_id": "api-open",
                        "cwd": str(project),
                        "tool_name": "Write",
                        "tool_input": {"file_path": "app.py", "content": "print('ok')"},
                    },
                )
                storage.record_event(event)
                port = server.server_address[1]
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/runs/{event['run_id']}/open",
                    data=json.dumps({"path": "app.py"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(raised.exception.code, 400)

                server.config.editor_command = (
                    f"{sys.executable} -c \"import pathlib,sys; pathlib.Path(sys.argv[2]).write_text(sys.argv[1])\" "
                    f"{{path}} {marker}"
                )
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/runs/{event['run_id']}/open",
                    data=json.dumps({"path": "README.md"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(raised.exception.code, 400)

                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/runs/{event['run_id']}/open",
                    data=json.dumps({"path": "app.py"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["opened"])
                deadline = time.time() + 2
                while time.time() < deadline and not marker.exists():
                    time.sleep(0.02)
                self.assertEqual(marker.read_text(encoding="utf-8"), str((project / "app.py").resolve()))
            finally:
                server.shutdown()
                server.stop_worker()
                server.server_close()
                storage.close()


def websocket_connect(host: str, port: int, path: str) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=2)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = sock.recv(4096)
    if b"101 Switching Protocols" not in response:
        raise AssertionError(response.decode("latin1", errors="replace"))
    return sock


def read_ws_json(sock: socket.socket) -> dict[str, object]:
    sock.settimeout(2)
    header = recv_exact(sock, 2)
    first, second = header
    opcode = first & 0x0F
    if opcode == 0x8:
        raise AssertionError("websocket closed")
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(recv_exact(sock, 8), "big")
    payload = recv_exact(sock, length)
    parsed = json.loads(payload.decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise AssertionError("socket closed")
        chunks.extend(chunk)
    return bytes(chunks)


if __name__ == "__main__":
    unittest.main()
