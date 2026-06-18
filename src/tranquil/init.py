from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import TranquilConfig, default_home, load_config, save_config
from .util import shell_join


CLAUDE_EVENTS = {
    "PostToolUse": ("post-tool-use", True),
    "PreToolUse": ("pre-tool-use", True),
    "SessionStart": ("session-start", False),
    "SessionEnd": ("session-end", False),
    "UserPromptSubmit": ("user-prompt-submit", False),
    "SubagentStart": ("subagent-start", False),
    "SubagentStop": ("subagent-stop", False),
    "PostToolUseFailure": ("tool-failure", True),
    "PermissionRequest": ("permission-request", False),
    "PermissionDenied": ("permission-denied", False),
    "TaskCreated": ("task-created", False),
    "TaskCompleted": ("task-completed", False),
    "FileChanged": ("file-changed", False),
    "Stop": ("stop", False),
    "PreCompact": ("compact", False),
}

CODEX_EVENTS = {
    "SessionStart": True,
    "UserPromptSubmit": False,
    "PreToolUse": True,
    "PermissionRequest": True,
    "PostToolUse": True,
    "PreCompact": True,
    "PostCompact": True,
    "SubagentStart": True,
    "SubagentStop": True,
    "Stop": False,
}


@dataclass(slots=True)
class InitReport:
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        output: list[str] = []
        output.extend(f"changed: {item}" for item in self.changed)
        output.extend(f"unchanged: {item}" for item in self.unchanged)
        output.extend(f"removed: {item}" for item in self.removed)
        output.extend(f"note: {item}" for item in self.notes)
        return output


def run_init(
    agent: str = "all",
    scope: str = "user",
    undo: bool = False,
    home: Path | None = None,
    cwd: Path | None = None,
) -> InitReport:
    expected_home = (home or default_home()).expanduser()
    expected_config = expected_home / "config.json"
    had_config = expected_config.exists()
    before_config = expected_config.read_text(encoding="utf-8") if had_config else None
    config = load_config(home=home, create=True)
    save_config(config)
    report = InitReport()
    after_config = config.config_path.read_text(encoding="utf-8")
    if before_config != after_config:
        report.changed.append(str(config.config_path))
    else:
        report.unchanged.append(str(config.config_path))
    report.notes.append(f"events write to {config.db_path}")
    report.notes.append("run 'tranquil' to open the terminal Fleet view")
    if agent in {"all", "claude-code", "claude"}:
        settings_path = claude_settings_path(scope, cwd=cwd)
        if undo:
            undo_claude(settings_path, config, report)
        else:
            install_claude(settings_path, config, report)
    if agent in {"all", "codex"}:
        hooks_path = codex_hooks_path(scope, cwd=cwd)
        if undo:
            undo_codex(hooks_path, config, report)
        else:
            install_codex(hooks_path, config, report)
    report.notes.append("undo with: tranquil init --undo")
    return report


def claude_settings_path(scope: str, cwd: Path | None = None) -> Path:
    root = cwd or Path.cwd()
    if scope == "user":
        return Path("~/.claude/settings.json").expanduser()
    if scope == "project":
        return root / ".claude" / "settings.json"
    if scope == "local":
        return root / ".claude" / "settings.local.json"
    raise ValueError("scope must be user, project, or local")


def codex_hooks_path(scope: str, cwd: Path | None = None) -> Path:
    root = cwd or Path.cwd()
    if scope == "user":
        return Path("~/.codex/hooks.json").expanduser()
    if scope in {"project", "local"}:
        return root / ".codex" / "hooks.json"
    raise ValueError("scope must be user, project, or local")


def install_claude(settings_path: Path, config: TranquilConfig, report: InitReport) -> None:
    original = read_json_object(settings_path)
    updated = json.loads(json.dumps(original))
    remove_tranquil_hooks(updated, config)
    hooks = updated.setdefault("hooks", {})
    for event_name, (_slug, needs_matcher) in CLAUDE_EVENTS.items():
        event_hooks = hooks.setdefault(event_name, [])
        entry: dict[str, Any] = {
            "hooks": [
                {
                    "type": "command",
                    "command": forward_command(config, event_name, agent="claude-code"),
                    "timeout": 10,
                }
            ]
        }
        if needs_matcher:
            entry["matcher"] = "*"
        event_hooks.append(entry)
    updated["_tranquil"] = {
        "managed": True,
        "version": "0.1.0",
        "home": str(config.home),
        "events": sorted(CLAUDE_EVENTS),
    }
    mcp_servers = updated.setdefault("mcpServers", {})
    mcp_servers["tranquil"] = {
        "command": "tranquil",
        "args": ["--home", str(config.home), "mcp"],
    }
    if updated == original:
        report.unchanged.append(str(settings_path))
        return
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report.changed.append(str(settings_path))


def undo_claude(settings_path: Path, config: TranquilConfig, report: InitReport) -> None:
    if not settings_path.exists():
        report.unchanged.append(f"{settings_path} (missing)")
        return
    original = read_json_object(settings_path)
    updated = json.loads(json.dumps(original))
    remove_tranquil_hooks(updated, config)
    remove_tranquil_mcp(updated)
    updated.pop("_tranquil", None)
    if updated == original:
        report.unchanged.append(str(settings_path))
        return
    settings_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report.removed.append(str(settings_path))


def install_codex(hooks_path: Path, config: TranquilConfig, report: InitReport) -> None:
    original = read_json_object(hooks_path)
    updated = json.loads(json.dumps(original))
    remove_tranquil_hooks(updated, config)
    hooks = updated.setdefault("hooks", {})
    for event_name, needs_matcher in CODEX_EVENTS.items():
        event_hooks = hooks.setdefault(event_name, [])
        entry: dict[str, Any] = {
            "hooks": [
                {
                    "type": "command",
                    "command": forward_command(config, event_name, agent="codex"),
                    "timeout": 5,
                    "statusMessage": "Sending event to Tranquil",
                }
            ]
        }
        if needs_matcher:
            entry["matcher"] = "*"
        event_hooks.append(entry)
    updated["_tranquil"] = {
        "managed": True,
        "version": "0.1.0",
        "home": str(config.home),
        "events": sorted(CODEX_EVENTS),
    }
    if updated == original:
        report.unchanged.append(str(hooks_path))
        return
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report.changed.append(str(hooks_path))
    report.notes.append("codex hooks may require review with /hooks before Codex runs them")


def undo_codex(hooks_path: Path, config: TranquilConfig, report: InitReport) -> None:
    if not hooks_path.exists():
        report.unchanged.append(f"{hooks_path} (missing)")
        return
    original = read_json_object(hooks_path)
    updated = json.loads(json.dumps(original))
    remove_tranquil_hooks(updated, config)
    updated.pop("_tranquil", None)
    if updated == original:
        report.unchanged.append(str(hooks_path))
        return
    hooks_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report.removed.append(str(hooks_path))


def forward_command(config: TranquilConfig, event_name: str, agent: str) -> str:
    """Command-hook invocation that writes the event straight to SQLite.

    Uses ``python -m tranquil.hook_forwarder`` so the per-event process imports
    only the lightweight capture path (no rich, no http server, no tui).
    """
    return shell_join(
        [
            sys.executable,
            "-m",
            "tranquil.hook_forwarder",
            "--home",
            str(config.home),
            "--agent",
            agent,
            "--event",
            event_name,
        ]
    )


def remove_tranquil_hooks(settings: dict[str, Any], config: TranquilConfig) -> None:
    """Remove Tranquil-managed hook entries.

    Matches both the new command hooks and any legacy HTTP collector hooks, so a
    re-run of ``init`` (or ``--undo``) cleanly upgrades older installs.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    prefixes = {f"{config.url}/hooks/", "http://127.0.0.1:8787/hooks/"}
    empty_events: list[str] = []
    for event_name, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        kept = [entry for entry in entries if not entry_is_tranquil(entry, prefixes)]
        hooks[event_name] = kept
        if not kept:
            empty_events.append(event_name)
    for event_name in empty_events:
        hooks.pop(event_name, None)
    if not hooks:
        settings.pop("hooks", None)


def remove_tranquil_mcp(settings: dict[str, Any]) -> None:
    servers = settings.get("mcpServers")
    if not isinstance(servers, dict):
        return
    tranquil = servers.get("tranquil")
    if isinstance(tranquil, dict) and tranquil.get("command") == "tranquil":
        servers.pop("tranquil", None)
    if not servers:
        settings.pop("mcpServers", None)


def entry_is_tranquil(entry: Any, prefixes: set[str]) -> bool:
    if not isinstance(entry, dict):
        return False
    hook_items = entry.get("hooks")
    if not isinstance(hook_items, list):
        return False
    for hook in hook_items:
        if not isinstance(hook, dict):
            continue
        url = str(hook.get("url", ""))
        if any(url.startswith(prefix) for prefix in prefixes):
            return True
        command = str(hook.get("command", ""))
        if "tranquil" in command and ("hook_forwarder" in command or "hook-forward" in command):
            return True
    return False


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value
