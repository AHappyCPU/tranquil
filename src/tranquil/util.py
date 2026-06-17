from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any


SECRET_KEY_RE = re.compile(r"(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)", re.I)
SECRET_VALUE_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?im)^([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*=).+$"),
]


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(value: Any) -> str:
    if not value:
        return iso_now()
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except ValueError:
        return iso_now()


def parse_iso(value: str) -> dt.datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def stable_id(prefix: str, *parts: Any, length: int = 20) -> str:
    digest = hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8")).digest()
    text = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return f"{prefix}_{text[:length]}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = value
        for pattern in SECRET_VALUE_PATTERNS:
            text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]", text)
        return text
    return value


def compact_json_for_fingerprint(value: Any) -> str:
    redacted = redact(value)
    if isinstance(redacted, str):
        return " ".join(redacted.split()).lower()
    return json_dumps(redacted).lower()


def safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def git_context(cwd: str | None) -> tuple[str | None, str | None]:
    if not cwd:
        return None, None
    path = Path(cwd).expanduser()
    if not path.exists():
        return None, None
    try:
        top = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).stdout.strip()
        return Path(top).name if top else None, branch or None
    except (OSError, subprocess.SubprocessError):
        return None, None


def git_repo_state(cwd: str | None) -> dict[str, Any]:
    if not cwd:
        return {}
    path = Path(cwd).expanduser()
    if not path.exists():
        return {"cwd": str(path)}
    state: dict[str, Any] = {"cwd": str(path)}
    try:
        top = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).stdout.strip()
        sha = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        ).stdout
        state.update(
            {
                "git_root": top,
                "repo": Path(top).name if top else None,
                "sha": sha,
                "branch": branch,
                "dirty": bool(status.strip()),
                "dirty_status": status,
            }
        )
        if status.strip():
            dirty_patch = subprocess.run(
                ["git", "-C", str(path), "diff", "--binary", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5.0,
            ).stdout
            if dirty_patch:
                state["dirty_patch"] = dirty_patch
    except (OSError, subprocess.SubprocessError):
        pass
    return state


def command_looks_like_test(command: str) -> bool:
    lowered = command.lower()
    test_terms = [
        "pytest",
        "unittest",
        "vitest",
        "jest",
        "cargo test",
        "go test",
        "make test",
        "npm test",
        "npm run test",
        "pnpm test",
        "pnpm run test",
        "yarn test",
        "yarn run test",
        "bun test",
        "bun run test",
        "tox",
        "nox",
        "rspec",
        "phpunit",
        "mvn test",
        "gradle test",
        "./gradlew test",
    ]
    return any(term in lowered for term in test_terms)


def command_looks_like_build(command: str) -> bool:
    lowered = command.lower()
    build_terms = [
        "npm run build",
        "pnpm build",
        "pnpm run build",
        "yarn build",
        "yarn run build",
        "bun run build",
        "cargo build",
        "go build",
        "make build",
        "cmake --build",
        "mvn package",
        "gradle build",
        "./gradlew build",
        "typecheck",
        "tsc",
        "mypy",
    ]
    return any(term in lowered for term in build_terms)


def command_looks_like_check(command: str) -> bool:
    lowered = command.lower()
    lint_terms = [
        "ruff",
        "eslint",
        "flake8",
        "pylint",
        "npm run lint",
        "pnpm lint",
        "pnpm run lint",
        "yarn lint",
        "yarn run lint",
        "go vet",
    ]
    return command_looks_like_test(command) or command_looks_like_build(command) or any(term in lowered for term in lint_terms)


def command_looks_like_pr_or_commit(command: str) -> bool:
    lowered = command.lower()
    return any(term in lowered for term in ["gh pr create", "git commit", "git push", "hub pull-request"])
