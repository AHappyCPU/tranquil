from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

from .config import TranquilConfig
from .storage import Storage, extract_paths, extract_tool_command


def pre_tool_decision(storage: Storage, config: TranquilConfig, event: dict[str, Any]) -> str | None:
    """Return a deny reason string for a PreToolUse event, or None to allow.

    This is the enforcement point: forbidden-path/command policy, a manual stop
    request, and the optional cost kill switch. It runs inside the command-hook
    process, so it must be fast and must never raise into the agent loop.
    """
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
