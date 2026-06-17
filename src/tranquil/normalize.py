from __future__ import annotations

from typing import Any

from .util import git_context, new_id, parse_ts, redact, safe_float, safe_int, stable_id


EVENT_ALIASES = {
    "sessionstart": "session_start",
    "session-start": "session_start",
    "session_start": "session_start",
    "sessionend": "session_end",
    "session-end": "session_end",
    "session_end": "session_end",
    "userpromptsubmit": "user_prompt",
    "user-prompt-submit": "user_prompt",
    "user_prompt": "user_prompt",
    "user": "user_prompt",
    "user-message": "user_prompt",
    "user_message": "user_prompt",
    "assistant": "stop",
    "assistant-message": "stop",
    "assistant_message": "stop",
    "pretooluse": "pre_tool",
    "pre-tool-use": "pre_tool",
    "pre_tool_use": "pre_tool",
    "pre_tool": "pre_tool",
    "posttooluse": "post_tool",
    "post-tool-use": "post_tool",
    "post_tool_use": "post_tool",
    "post_tool": "post_tool",
    "posttoolusefailure": "tool_failure",
    "post-tool-use-failure": "tool_failure",
    "tool-failure": "tool_failure",
    "tool_failure": "tool_failure",
    "permissionrequest": "permission_request",
    "permission-request": "permission_request",
    "permissiondenied": "permission_denied",
    "permission-denied": "permission_denied",
    "subagentstart": "subagent_start",
    "subagent-start": "subagent_start",
    "subagentstop": "subagent_stop",
    "subagent-stop": "subagent_stop",
    "taskcreated": "task_created",
    "task-created": "task_created",
    "taskcompleted": "task_completed",
    "task-completed": "task_completed",
    "filechanged": "file_changed",
    "file-changed": "file_changed",
    "stop": "stop",
    "precompact": "compact",
    "pre-compact": "compact",
    "compact": "compact",
}


def normalize_event(event_hint: str, payload: dict[str, Any], source: str = "hook") -> dict[str, Any]:
    event_type = normalize_event_type(
        payload.get("event_type")
        or payload.get("hook_event_name")
        or payload.get("hookEventName")
        or payload.get("type")
        or event_hint
    )
    agent = normalize_agent(payload.get("agent") or payload.get("vendor") or payload.get("source_agent") or payload.get("app"))
    ts = parse_ts(payload.get("ts") or payload.get("timestamp") or payload.get("created_at") or payload.get("time"))
    session_id = str(payload.get("session_id") or payload.get("sessionId") or payload.get("conversation_id") or payload.get("rollout_id") or stable_id("session", agent, ts, payload.get("transcript_path") or payload.get("cwd")))
    parent_session_id = payload.get("parent_session_id") or payload.get("parentSessionId") or payload.get("parent_id")
    if parent_session_id is not None:
        parent_session_id = str(parent_session_id)
    run_seed = payload.get("run_id") or payload.get("runId") or parent_session_id or session_id
    run_id = str(payload.get("run_id") or payload.get("runId") or stable_id("run", agent, run_seed))
    cwd = extract_cwd(payload)
    repo, branch = extract_repo_branch(payload, cwd)
    tool = extract_tool(payload)
    usage = extract_usage(payload)
    permission = extract_permission(payload)
    message = extract_message(payload)
    labels = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}

    return {
        "schema": "tranquil.event/v1",
        "event_id": str(payload.get("event_id") or payload.get("id") or new_id("evt")),
        "ts": ts,
        "source": source,
        "agent": agent,
        "agent_version": payload.get("agent_version") or payload.get("version"),
        "event_type": event_type,
        "run_id": run_id,
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "depth": safe_int(payload.get("depth") or payload.get("subagent_depth")) or 0,
        "context": {
            "cwd": cwd,
            "repo": repo,
            "branch": branch,
            "worktree": payload.get("worktree") or payload.get("worktree_path"),
            "permission_mode": payload.get("permission_mode") or payload.get("permissionMode"),
        },
        "model": payload.get("model") or payload.get("requested_model") or payload.get("requestedModel"),
        "resolved_model": payload.get("resolved_model") or payload.get("resolvedModel") or payload.get("actual_model") or payload.get("model"),
        "tool": tool,
        "usage": usage,
        "permission": permission,
        "labels": labels,
        "message": message,
        "raw": redact(payload),
    }


def normalize_event_type(value: Any) -> str:
    key = str(value or "event").strip()
    folded = key.replace("_", "-").lower()
    return EVENT_ALIASES.get(folded, EVENT_ALIASES.get(key.lower(), folded.replace("-", "_")))


def normalize_agent(value: Any) -> str:
    text = str(value or "").lower()
    if "codex" in text or text in {"cx", "openai"}:
        return "codex"
    return "claude-code"


def extract_cwd(payload: dict[str, Any]) -> str | None:
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    return payload.get("cwd") or context.get("cwd") or payload.get("working_directory") or payload.get("workdir")


def extract_repo_branch(payload: dict[str, Any], cwd: str | None) -> tuple[str | None, str | None]:
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    repo = payload.get("repo") or context.get("repo")
    branch = payload.get("branch") or context.get("branch")
    if repo and branch:
        return str(repo), str(branch)
    git_repo, git_branch = git_context(cwd)
    return str(repo or git_repo) if repo or git_repo else None, str(branch or git_branch) if branch or git_branch else None


def extract_tool(payload: dict[str, Any]) -> dict[str, Any] | None:
    raw_tool = payload.get("tool") if isinstance(payload.get("tool"), dict) else {}
    name = payload.get("tool_name") or payload.get("toolName") or raw_tool.get("name") or payload.get("name")
    tool_input = payload.get("tool_input") or payload.get("toolInput") or raw_tool.get("input") or payload.get("input")
    output = payload.get("tool_output") or payload.get("toolOutput") or raw_tool.get("output") or payload.get("output") or payload.get("result")
    duration_ms = safe_int(payload.get("duration_ms") or payload.get("durationMs") or raw_tool.get("duration_ms") or raw_tool.get("durationMs"))
    exit_code_value = payload.get("exit_code")
    if exit_code_value is None:
        exit_code_value = raw_tool.get("exit_code")
    exit_code = safe_int(exit_code_value)
    if name is None and tool_input is None and output is None and duration_ms is None:
        return None
    output_payload: dict[str, Any] | str | None
    if isinstance(output, dict):
        output_payload = dict(output)
    else:
        output_payload = output
    if exit_code is not None:
        if isinstance(output_payload, dict):
            output_payload.setdefault("exit_code", exit_code)
        else:
            output_payload = {"summary": output_payload, "exit_code": exit_code}
    return {
        "name": str(name or "unknown"),
        "input": tool_input,
        "output": output_payload,
        "duration_ms": duration_ms,
    }


def extract_usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    cost = (
        usage.get("cost_usd_est")
        or usage.get("total_cost_usd")
        or usage.get("cost_usd")
        or payload.get("cost_usd_est")
        or payload.get("total_cost_usd")
    )
    return {
        "input_tokens": safe_int(usage.get("input_tokens") or usage.get("inputTokens") or payload.get("input_tokens")),
        "output_tokens": safe_int(usage.get("output_tokens") or usage.get("outputTokens") or payload.get("output_tokens")),
        "cache_read_tokens": safe_int(usage.get("cache_read_tokens") or usage.get("cacheReadTokens")),
        "cache_write_tokens": safe_int(usage.get("cache_write_tokens") or usage.get("cacheWriteTokens")),
        "cost_usd_est": safe_float(cost),
    }


def extract_permission(payload: dict[str, Any]) -> dict[str, Any] | None:
    raw = payload.get("permission") if isinstance(payload.get("permission"), dict) else {}
    decision = payload.get("permission_decision") or payload.get("permissionDecision") or raw.get("decision")
    reason = payload.get("permission_reason") or payload.get("permissionReason") or raw.get("reason")
    if decision is None and reason is None:
        return None
    return {"decision": decision, "reason": reason}


def extract_message(payload: dict[str, Any]) -> str | None:
    for key in ("message", "assistant_message", "assistantMessage", "final_text", "text", "content", "prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw_message = payload.get("message")
    if isinstance(raw_message, dict):
        content = raw_message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [part.get("text") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
            if parts:
                return "\n".join(parts).strip()
    tool = payload.get("tool") if isinstance(payload.get("tool"), dict) else {}
    output = payload.get("tool_output") or tool.get("output")
    if isinstance(output, str) and output.strip():
        return output.strip()[:2000]
    if isinstance(output, dict):
        for key in ("summary", "text", "message", "content"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:2000]
    return None
