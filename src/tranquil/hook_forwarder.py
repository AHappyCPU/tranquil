from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .normalize import normalize_event
from .notifications import SignalNotifier
from .policy import pre_tool_decision
from .storage import Storage


COMPLETED_EVENT_TYPES = {"session_end", "stop", "task_completed"}


def main(
    argv: list[str] | None = None,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    """Ingest a single agent hook event into the local SQLite store.

    Both Claude Code and Codex are wired to run this as a command hook. It reads
    the hook JSON on stdin and writes a normalized event straight to SQLite — no
    network, no collector, no port. It is strictly fail-open: any error is logged
    to stderr and the process still exits 0 so the agent loop is never blocked.
    """
    parser = argparse.ArgumentParser(prog="tranquil-hook")
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--event", required=True)
    parser.add_argument("--agent", default=None, help="Source agent: claude-code or codex.")
    # Accept and ignore legacy flags so upgraded hook configs keep working.
    parser.add_argument("--url", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        return _ingest(args, stdin, stdout)
    except Exception as exc:  # fail-open: a capture error must never break the agent
        print(f"tranquil-hook: {type(exc).__name__}: {exc}", file=stderr)
        return 0


def _ingest(args: argparse.Namespace, stdin: Any, stdout: Any) -> int:
    payload = parse_payload(stdin.read())
    if args.agent:
        payload.setdefault("agent", args.agent)
    payload.setdefault("hook_event_name", args.event)
    config = load_config(home=args.home, create=True)
    notifier = SignalNotifier(config)
    sink = notifier.deliver_sync if notifier.enabled else None
    storage = Storage(
        config.db_path,
        thresholds=config.signal_thresholds,
        raw_payloads=config.raw_payloads,
        signal_sink=sink,
    )
    try:
        event = normalize_event(args.event, payload, source="hook")
        if event.get("event_type") == "pre_tool":
            decision = pre_tool_decision(storage, config, event)
            if decision:
                event["permission"] = {"decision": "deny", "reason": decision}
                storage.record_event(event)
                write_decision(stdout, decision)
                return 0
        storage.record_event(event)
        maybe_sample_trace(storage, config, event)
    finally:
        storage.close()
    return 0


def maybe_sample_trace(storage: Storage, config: Any, event: dict[str, Any]) -> None:
    if not config.trace_sampling_enabled:
        return
    if event.get("event_type") not in COMPLETED_EVENT_TYPES:
        return
    storage.sample_run_if_eligible(
        event["run_id"],
        suite=config.trace_sample_suite,
        sample_rate=config.trace_sample_rate,
    )


def write_decision(stdout: Any, reason: str) -> None:
    """Emit a deny decision for a PreToolUse hook.

    Covers both the older top-level ``decision`` shape and the newer
    ``hookSpecificOutput`` shape so whichever agent honors it can block the call.
    """
    stdout.write(
        json.dumps(
            {
                "decision": "block",
                "reason": reason,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    stdout.flush()


def parse_payload(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"stdin": raw}
    return payload if isinstance(payload, dict) else {"stdin": payload}


if __name__ == "__main__":
    raise SystemExit(main())
