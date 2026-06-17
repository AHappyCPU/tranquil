from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import queue
import shlex
import signal
import socket
import subprocess
import threading
from fnmatch import fnmatch
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .config import TranquilConfig
from .evals import replay_fixture
from .normalize import normalize_event
from .notifications import SignalNotifier
from .signals import scan_idle_runs
from .storage import Storage, extract_paths, extract_tool_command
from .tailer import RolloutTailer, TranscriptTailer


MAX_BODY_BYTES = 2 * 1024 * 1024
STATIC_DIR = Path(__file__).with_name("web")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketHub:
    def __init__(self) -> None:
        self._clients: set[socket.socket] = set()
        self._lock = threading.Lock()

    def add(self, sock: socket.socket) -> None:
        with self._lock:
            self._clients.add(sock)

    def remove(self, sock: socket.socket) -> None:
        with self._lock:
            self._clients.discard(sock)

    def broadcast(self, payload: dict[str, Any]) -> None:
        frame = websocket_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        with self._lock:
            clients = list(self._clients)
        dead: list[socket.socket] = []
        for client in clients:
            try:
                client.sendall(frame)
            except OSError:
                dead.append(client)
        for client in dead:
            self.remove(client)


class EventWorker(threading.Thread):
    def __init__(
        self,
        storage: Storage,
        events: "queue.Queue[tuple[str, dict[str, Any], str]]",
        hub: WebSocketHub,
        trace_sampler: Callable[[dict[str, Any]], None] | None = None,
    ):
        super().__init__(name="tranquil-event-worker", daemon=True)
        self.storage = storage
        self.events = events
        self.hub = hub
        self.trace_sampler = trace_sampler
        self.errors: list[str] = []
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                event_hint, payload, source = self.events.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                event = normalize_event(event_hint, payload, source=source)
                inserted = self.storage.record_event(event)
                if inserted:
                    self.hub.broadcast(event_notification(event))
                    if self.trace_sampler:
                        self.trace_sampler(event)
            except Exception as exc:  # pragma: no cover - visible through /api/health
                self.errors.append(f"{type(exc).__name__}: {exc}")
                del self.errors[:-20]
            finally:
                self.events.task_done()


class TranquilHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: TranquilConfig, storage: Storage, notifier: SignalNotifier | None = None):
        self.config = config
        self.storage = storage
        self.notifier = notifier
        self.event_queue: "queue.Queue[tuple[str, dict[str, Any], str]]" = queue.Queue(maxsize=10000)
        self.ws_hub = WebSocketHub()
        self.worker = EventWorker(storage, self.event_queue, self.ws_hub, trace_sampler=self.sample_completed_trace)
        self.tailer = TranscriptTailer(storage, config.transcript_paths, config.tail_interval_seconds) if config.transcript_paths else None
        self.rollout_tailer = (
            RolloutTailer(storage, config.codex_rollout_paths, config.tail_interval_seconds) if config.codex_rollout_paths else None
        )
        self.editor_processes: list[subprocess.Popen[Any]] = []
        self.editor_processes_lock = threading.Lock()
        super().__init__((config.host, config.port), TranquilHandler)

    def sample_completed_trace(self, event: dict[str, Any]) -> None:
        if not self.config.trace_sampling_enabled:
            return
        if event.get("event_type") not in {"session_end", "stop", "task_completed"}:
            return
        fixture = self.storage.sample_run_if_eligible(
            event["run_id"],
            suite=self.config.trace_sample_suite,
            sample_rate=self.config.trace_sample_rate,
        )
        if fixture:
            self.ws_hub.broadcast({"type": "fixture", "fixture_id": fixture["fixture_id"], "run_id": event["run_id"]})

    def start_worker(self) -> None:
        if not self.worker.is_alive():
            self.worker.start()
        if self.tailer and not self.tailer.is_alive():
            self.tailer.start()
        if self.rollout_tailer and not self.rollout_tailer.is_alive():
            self.rollout_tailer.start()

    def stop_worker(self) -> None:
        self.worker.stop()
        if self.tailer:
            self.tailer.stop()
        if self.rollout_tailer:
            self.rollout_tailer.stop()
        self.cleanup_editor_processes()

    def track_editor_process(self, process: subprocess.Popen[Any]) -> None:
        with self.editor_processes_lock:
            self.editor_processes = [item for item in self.editor_processes if item.poll() is None]
            self.editor_processes.append(process)

    def cleanup_editor_processes(self) -> None:
        with self.editor_processes_lock:
            remaining = []
            for process in self.editor_processes:
                if process.poll() is None:
                    remaining.append(process)
                else:
                    process.wait(timeout=0)
            self.editor_processes = remaining


class TranquilHandler(BaseHTTPRequestHandler):
    server: TranquilHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        # Keep normal CLI output calm. The API returns explicit health details.
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                self.write_json(
                    {
                        "ok": True,
                        "queue_depth": self.server.event_queue.qsize(),
                        "worker_alive": self.server.worker.is_alive(),
                        "worker_errors": self.server.worker.errors[-5:],
                        "tailer_alive": self.server.tailer.is_alive() if self.server.tailer else False,
                        "tailer_errors": self.server.tailer.errors[-5:] if self.server.tailer else [],
                        "rollout_tailer_alive": self.server.rollout_tailer.is_alive() if self.server.rollout_tailer else False,
                        "rollout_tailer_errors": self.server.rollout_tailer.errors[-5:] if self.server.rollout_tailer else [],
                        "notifications_enabled": bool(self.server.notifier and self.server.notifier.enabled),
                        "notification_errors": self.server.notifier.errors[-5:] if self.server.notifier else [],
                        "trace_sampling_enabled": self.server.config.trace_sampling_enabled,
                        "trace_sample_rate": self.server.config.trace_sample_rate,
                        "trace_sample_suite": self.server.config.trace_sample_suite,
                    }
                )
            elif path == "/api/stats":
                self.write_json(self.server.storage.stats())
            elif path == "/api/cost":
                group_by = first(query.get("group_by"), "agent") or "agent"
                self.write_json({"cost": self.server.storage.cost_rollup(group_by=group_by)})
            elif path == "/api/evals":
                suite = first(query.get("suite"), None)
                limit = int(first(query.get("limit"), "20"))
                self.write_json({"eval_runs": self.server.storage.list_eval_runs(suite=suite, limit=limit)})
            elif path.startswith("/api/evals/"):
                eval_run_id = path.rsplit("/", 1)[-1]
                eval_run = self.server.storage.get_eval_run(eval_run_id)
                if not eval_run:
                    self.write_json({"error": "eval run not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                self.write_json({"eval_run": eval_run, "scores": self.server.storage.list_scores(eval_run_id)})
            elif path == "/api/runs":
                scan_idle_runs(self.server.storage, self.server.config.signal_thresholds)
                limit = int(first(query.get("limit"), "50"))
                status = first(query.get("status"), None)
                agent = first(query.get("agent"), None)
                repo = first(query.get("repo"), None)
                branch = first(query.get("branch"), None)
                labels = query.get("label")
                self.write_json(
                    {
                        "runs": self.server.storage.list_runs(
                            limit=limit,
                            status=status,
                            agent=agent,
                            repo=repo,
                            branch=branch,
                            labels=labels,
                        )
                    }
                )
            elif path.startswith("/api/runs/"):
                parts = path.strip("/").split("/")
                if len(parts) == 5 and parts[0] == "api" and parts[1] == "runs" and parts[3] == "diff":
                    self.write_json({"diff": self.server.storage.diff_runs(parts[2], parts[4])})
                    return
                run_id = parts[-1]
                run = self.server.storage.get_run(run_id)
                if not run:
                    self.write_json({"error": "run not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                self.write_json(
                    {
                        "run": run,
                        "events": self.server.storage.get_run_display_events(run_id),
                        "signals": self.server.storage.list_run_signals(run_id),
                        "subagents": self.server.storage.list_subagents(run_id),
                        "files": self.server.storage.file_touch_summary(run_id),
                        "scores": self.server.storage.list_run_scores(run_id),
                    }
                )
            elif path == "/api/signals":
                scan_idle_runs(self.server.storage, self.server.config.signal_thresholds)
                active_value = first(query.get("active"), "true")
                active = None if active_value == "all" else active_value.lower() not in {"0", "false", "no"}
                self.write_json({"signals": self.server.storage.list_signals(active=active)})
            elif path == "/api/fixtures":
                suite = first(query.get("suite"), None)
                self.write_json({"fixtures": self.server.storage.list_fixtures(suite=suite)})
            elif path == "/ws":
                self.handle_websocket()
            else:
                self.serve_static(path)
        except Exception as exc:  # pragma: no cover - defensive API boundary
            self.write_json({"error": f"{type(exc).__name__}: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/hooks/"):
                self.handle_hook(path.rsplit("/", 1)[-1])
            elif path == "/api/events":
                payload = self.read_json_body()
                event_hint = str(payload.pop("event_hint", payload.get("event_type", "event")))
                event = normalize_event(event_hint, payload, source="api")
                inserted = self.server.storage.record_event(event)
                if inserted:
                    self.server.ws_hub.broadcast(event_notification(event))
                self.write_json({"inserted": inserted, "event_id": event["event_id"], "run_id": event["run_id"]})
            elif path.startswith("/api/runs/") and path.endswith("/open"):
                self.handle_run_open(path)
            elif path.startswith("/api/runs/") and path.endswith("/replay"):
                self.handle_run_replay(path)
            elif path.startswith("/api/fixtures/"):
                run_id = path.rsplit("/", 1)[-1]
                fixture = self.server.storage.create_fixture(run_id)
                self.write_json({"fixture": fixture}, status=HTTPStatus.CREATED)
            else:
                self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.UNAUTHORIZED)
        except queue.Full:
            # Fail open for the agent: accept the hook and let transcript backfill
            # recover later instead of holding the agent loop.
            self.write_json({"queued": False, "reason": "queue_full"})
        except ValueError as exc:
            self.write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive API boundary
            self.write_json({"error": f"{type(exc).__name__}: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_hook(self, event_hint: str) -> None:
        if self.headers.get("Origin"):
            raise PermissionError("hook requests with browser Origin headers are rejected")
        expected = self.server.config.token
        if expected:
            header = self.headers.get("Authorization", "")
            if header != f"Bearer {expected}":
                raise PermissionError("missing or invalid bearer token")
        payload = self.read_json_body()
        if event_hint in {"pre-tool-use", "pre_tool", "PreToolUse"}:
            response = self.handle_pre_tool(event_hint, payload)
            self.write_json(response)
            return
        self.server.event_queue.put_nowait((event_hint, payload, "hook"))
        self.write_json({})

    def handle_pre_tool(self, event_hint: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = normalize_event(event_hint, payload, source="hook")
        decision = pre_tool_decision(self.server.storage, self.server.config, event)
        if decision:
            event["permission"] = {"decision": "deny", "reason": decision}
            inserted = self.server.storage.record_event(event)
            if inserted:
                self.server.ws_hub.broadcast(event_notification(event))
            return {
                "permissionDecision": "deny",
                "permissionDecisionReason": decision,
                "decision": "deny",
                "reason": decision,
            }
        inserted = self.server.storage.record_event(event)
        if inserted:
            self.server.ws_hub.broadcast(event_notification(event))
        return {}

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def handle_run_open(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "api" or parts[1] != "runs" or parts[3] != "open":
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        run_id = parts[2]
        run = self.server.storage.get_run(run_id)
        if not run:
            self.write_json({"error": "run not found"}, status=HTTPStatus.NOT_FOUND)
            return
        payload = self.read_json_body()
        requested_path = payload.get("path")
        if not isinstance(requested_path, str) or not requested_path:
            raise ValueError("open requires a touched file path")
        line = int(payload.get("line") or 1)
        files = self.server.storage.file_touch_summary(run_id)
        touched_paths = {file["path"] for file in files}
        if requested_path not in touched_paths:
            raise ValueError("open path was not touched by this run")
        editor_command = self.server.config.editor_command or os.environ.get("TRANQUIL_EDITOR_COMMAND")
        if not editor_command:
            raise ValueError("Open in editor requires editor_command in config.json or TRANQUIL_EDITOR_COMMAND.")
        events = self.server.storage.get_run_events(run_id)
        cwd = first_event_cwd(events)
        target = resolve_open_path(requested_path, cwd)
        args = build_editor_command(editor_command, target, line=line, cwd=cwd, run_id=run_id)
        process = subprocess.Popen(
            args,
            cwd=str(Path(cwd).expanduser()) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.server.track_editor_process(process)
        self.write_json({"opened": True, "path": str(target), "command": args[0]})

    def handle_run_replay(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "api" or parts[1] != "runs" or parts[3] != "replay":
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        run_id = parts[2]
        if not self.server.storage.get_run(run_id):
            self.write_json({"error": "run not found"}, status=HTTPStatus.NOT_FOUND)
            return
        payload = self.read_json_body()
        agent = str(payload.get("agent") or "command")
        if agent not in {"command", "codex"}:
            raise ValueError("replay agent must be 'command' or 'codex'")
        command = payload.get("command")
        if command is not None and not isinstance(command, str):
            raise ValueError("replay command must be a string")
        command = command or self.server.config.replay_command
        if not command and agent == "command":
            raise ValueError("Replay requires command, replay_command in config.json, or agent=codex.")
        model = payload.get("model")
        if model is not None and not isinstance(model, str):
            raise ValueError("model must be a string")
        config_path = payload.get("config_path") or payload.get("config")
        if config_path is not None and not isinstance(config_path, str):
            raise ValueError("config_path must be a string")
        if config_path and not Path(config_path).expanduser().exists():
            raise ValueError(f"Replay config path does not exist: {config_path}")
        suite = str(payload.get("suite") or "replay")
        fixture = self.server.storage.create_fixture(run_id, suite=suite)
        eval_run_id, scores = replay_fixture(
            self.server.storage,
            fixture["fixture_id"],
            command,
            self.server.config.home / "replays",
            agent=agent,
            model=model,
            config_path=config_path,
        )
        self.write_json({"fixture": fixture, "eval_run_id": eval_run_id, "scores": scores}, status=HTTPStatus.CREATED)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists() or not target.is_file():
            self.write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if relative == "index.html" else "max-age=60")
        self.end_headers()
        self.wfile.write(body)

    def handle_websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or self.headers.get("Upgrade", "").lower() != "websocket":
            self.write_json({"error": "websocket upgrade required"}, status=HTTPStatus.BAD_REQUEST)
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        sock = self.connection
        sock.settimeout(0.5)
        self.server.ws_hub.add(sock)
        try:
            sock.sendall(websocket_frame(json.dumps({"type": "hello", "ok": True}).encode("utf-8")))
            while not getattr(self.server, "_BaseServer__shutdown_request", False):
                try:
                    frame = read_websocket_frame(sock)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    sock.sendall(websocket_frame(payload, opcode=0xA))
        finally:
            self.server.ws_hub.remove(sock)

    def write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = (json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def first(values: list[str] | None, default: str | None) -> str | None:
    if not values:
        return default
    return values[0]


def first_event_cwd(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        context = event.get("context") if isinstance(event.get("context"), dict) else {}
        cwd = context.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def resolve_open_path(path: str, cwd: str | None) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        if not cwd:
            raise ValueError("open requires an absolute path or a recorded cwd")
        target = Path(cwd).expanduser() / target
    return target.resolve(strict=False)


def build_editor_command(command: str, path: Path, line: int, cwd: str | None, run_id: str) -> list[str]:
    parts = shlex.split(command)
    if not parts:
        raise ValueError("editor_command is empty")
    replacements = {
        "{path}": str(path),
        "{line}": str(max(1, line)),
        "{cwd}": str(Path(cwd).expanduser()) if cwd else "",
        "{run_id}": run_id,
    }
    replaced = []
    saw_path = False
    for part in parts:
        value = part
        for token, replacement in replacements.items():
            if token == "{path}" and token in value:
                saw_path = True
            value = value.replace(token, replacement)
        replaced.append(value)
    if not saw_path:
        replaced.append(str(path))
    return replaced


def event_notification(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "event",
        "event_id": event.get("event_id"),
        "run_id": event.get("run_id"),
        "event_type": event.get("event_type"),
        "agent": event.get("agent"),
        "ts": event.get("ts"),
    }


def websocket_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    first_byte = 0x80 | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        header = bytes([first_byte, length])
    elif length < 65536:
        header = bytes([first_byte, 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([first_byte, 127]) + length.to_bytes(8, "big")
    return header + payload


def read_websocket_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    header = recv_exact(sock, 2)
    if not header:
        return None
    first_byte, second_byte = header
    opcode = first_byte & 0x0F
    masked = bool(second_byte & 0x80)
    length = second_byte & 0x7F
    if length == 126:
        length = int.from_bytes(recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(recv_exact(sock, 8), "big")
    mask = recv_exact(sock, 4) if masked else b""
    payload = recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise OSError("websocket closed")
        chunks.extend(chunk)
    return bytes(chunks)


def pre_tool_decision(storage: Storage, config: TranquilConfig, event: dict[str, Any]) -> str | None:
    run = storage.get_run(event["run_id"])
    policy = policy_violation(config, event)
    if policy:
        if run:
            storage.add_signal(
                event["run_id"],
                "policy_denied",
                "high",
                {
                    **policy,
                    "fingerprint": policy.get("fingerprint") or policy.get("pattern") or policy.get("reason"),
                },
                action="deny_pre_tool",
            )
        return f"Tranquil policy: {policy['message']}"
    if run and storage.has_active_signal(event["run_id"], "stop_requested"):
        return "Tranquil stop requested: future tool calls for this run are denied."
    if not config.kill_switch_enabled:
        return None
    if not run:
        return None
    cost = float(run.get("total_cost_usd_est") or 0)
    if cost >= config.run_cost_budget_usd:
        storage.add_signal(
            event["run_id"],
            "runaway_cost",
            "high",
            {
                "reason": "pre_tool_kill_switch_budget",
                "cost_usd_est": cost,
                "budget_usd": config.run_cost_budget_usd,
                "fingerprint": "kill_switch",
            },
            action="deny_pre_tool",
        )
        return f"Tranquil kill switch: run cost ${cost:.2f} est. is over budget ${config.run_cost_budget_usd:.2f}."
    return None


def policy_violation(config: TranquilConfig, event: dict[str, Any]) -> dict[str, Any] | None:
    if not config.policy_enabled:
        return None
    paths = sorted(extract_paths(event))
    for path in paths:
        for pattern in config.policy_forbidden_paths:
            if fnmatch(path, pattern):
                return {
                    "reason": "forbidden_path",
                    "path": path,
                    "pattern": pattern,
                    "message": f"path {path} matches forbidden pattern {pattern}",
                    "fingerprint": f"path:{pattern}",
                }
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
    command = extract_tool_command(tool)
    if command:
        for pattern in config.policy_forbidden_commands:
            if pattern in command or fnmatch(command, pattern):
                return {
                    "reason": "forbidden_command",
                    "command": command,
                    "pattern": pattern,
                    "message": f"command matches forbidden pattern {pattern}",
                    "fingerprint": f"command:{pattern}",
                }
    return None


def serve(config: TranquilConfig) -> None:
    notifier = SignalNotifier(config)
    storage = Storage(
        config.db_path,
        thresholds=config.signal_thresholds,
        raw_payloads=config.raw_payloads,
        signal_sink=notifier.notify_signal,
    )
    httpd = TranquilHTTPServer(config, storage, notifier=notifier)
    httpd.start_worker()
    stopping = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stopping.set()
        threading.Thread(target=httpd.shutdown, name="tranquil-shutdown", daemon=True).start()

    old_int = signal.signal(signal.SIGINT, stop)
    old_term = signal.signal(signal.SIGTERM, stop)
    try:
        print(f"Tranquil is running at {config.url}")
        print(f"SQLite: {config.db_path}")
        print("Press Ctrl-C to stop.")
        httpd.serve_forever(poll_interval=0.25)
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        httpd.stop_worker()
        httpd.server_close()
        storage.close()
        if not stopping.is_set():
            stopping.set()


def run_terminal_app(config: TranquilConfig, interval: float = 2.0) -> int:
    from .tui import run_tui

    notifier = SignalNotifier(config)
    storage = Storage(
        config.db_path,
        thresholds=config.signal_thresholds,
        raw_payloads=config.raw_payloads,
        signal_sink=notifier.notify_signal,
    )
    httpd = TranquilHTTPServer(config, storage, notifier=notifier)
    httpd.start_worker()
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.25}, name="tranquil-http", daemon=True)
    thread.start()
    try:
        return run_tui(storage, config.signal_thresholds, interval=interval)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.stop_worker()
        httpd.server_close()
        storage.close()
