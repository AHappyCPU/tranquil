from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import load_config


EVENT_SLUGS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "user-prompt-submit",
    "PreToolUse": "pre-tool-use",
    "PermissionRequest": "permission-request",
    "PostToolUse": "post-tool-use",
    "PreCompact": "compact",
    "PostCompact": "post-compact",
    "SubagentStart": "subagent-start",
    "SubagentStop": "subagent-stop",
    "Stop": "stop",
}


def main(argv: list[str] | None = None, stdin: Any = None, stdout: Any = None, stderr: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="tranquil hook-forward")
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--event", required=True)
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("--url", default=None)
    args = parser.parse_args(argv)
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    config = load_config(home=args.home, create=True)
    raw = stdin.read()
    payload = parse_payload(raw)
    payload.setdefault("agent", "codex")
    payload.setdefault("hook_event_name", args.event)
    url = args.url or f"{config.url}/hooks/{EVENT_SLUGS.get(args.event, args.event)}"
    try:
        response = post_hook(url, payload, token=config.token, timeout=args.timeout)
    except Exception as exc:
        # Hooks must be fail-open. A collector outage should never make Codex
        # fail or slow the user-visible agent loop.
        print(f"tranquil hook-forward: {type(exc).__name__}: {exc}", file=stderr)
        return 0
    if response:
        stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        stdout.flush()
    return 0


def parse_payload(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"stdin": raw}
    return payload if isinstance(payload, dict) else {"stdin": payload}


def post_hook(url: str, payload: dict[str, Any], token: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    if not body.strip():
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {"output": body}
    return parsed if isinstance(parsed, dict) else {"output": parsed}


if __name__ == "__main__":
    raise SystemExit(main())

