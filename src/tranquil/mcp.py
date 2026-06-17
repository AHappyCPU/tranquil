from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any, Callable

from .storage import Storage
from .util import now_utc, parse_ts


ToolHandler = Callable[[dict[str, Any]], Any]


def run_mcp_server(storage: Storage, stdin: Any = None, stdout: Any = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    handlers: dict[str, ToolHandler] = {
        "tranquil_query_runs": lambda args: storage.list_runs(
            limit=int(args.get("limit", 20)),
            status=args.get("status"),
            agent=args.get("agent"),
            repo=args.get("repo"),
            branch=args.get("branch"),
            labels=args.get("label"),
            since=args.get("since"),
        ),
        "tranquil_get_run": lambda args: get_run(storage, required(args, "run_id")),
        "tranquil_diff_runs": lambda args: storage.diff_runs(required(args, "a"), required(args, "b")),
        "tranquil_cost": lambda args: storage.cost_rollup(
            group_by=args.get("group_by", "agent"),
            since=args.get("since") or since_from_window(args.get("window")),
        ),
        "tranquil_signals": lambda args: storage.list_signals(active=args.get("active", True)),
        "tranquil_eval_status": lambda args: eval_status(storage, args.get("suite")),
    }
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request, handlers)
        except Exception as exc:
            response = error_response(None, -32603, f"{type(exc).__name__}: {exc}")
        if response is None:
            continue
        stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        stdout.flush()
    return 0


def handle_request(request: dict[str, Any], handlers: dict[str, ToolHandler]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if request_id is None and str(method).startswith("notifications/"):
        return None
    if method == "initialize":
        return result_response(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "serverInfo": {"name": "tranquil", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            },
        )
    if method == "tools/list":
        return result_response(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name not in handlers:
            return error_response(request_id, -32602, f"unknown tool: {name}")
        result = handlers[name](arguments)
        return result_response(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, indent=2, sort_keys=True, default=str),
                    }
                ],
                "isError": False,
            },
        )
    if method == "ping":
        return result_response(request_id, {})
    return error_response(request_id, -32601, f"unknown method: {method}")


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "tranquil_query_runs",
            "description": "Return recent Tranquil run summaries.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "agent": {"type": "string", "enum": ["claude-code", "codex"]},
                    "repo": {"type": "string"},
                    "branch": {"type": "string"},
                    "label": {"type": "string"},
                    "since": {"type": "string", "description": "UTC ISO timestamp lower bound for run last activity."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
        },
        {
            "name": "tranquil_get_run",
            "description": "Return a run summary, normalized transcript events, and signals.",
            "inputSchema": {
                "type": "object",
                "required": ["run_id"],
                "properties": {"run_id": {"type": "string"}},
            },
        },
        {
            "name": "tranquil_diff_runs",
            "description": "Compare cost, tool calls, files, signals, and tool sequence for two runs.",
            "inputSchema": {
                "type": "object",
                "required": ["a", "b"],
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            },
        },
        {
            "name": "tranquil_cost",
            "description": "Return estimated cost rollups grouped by agent, repo, branch, or status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "group_by": {"type": "string", "enum": ["agent", "repo", "branch", "status"]},
                    "window": {"type": "string", "description": "Relative window such as 24h, 7d, or 30m."},
                    "since": {"type": "string", "description": "UTC ISO timestamp lower bound."},
                },
            },
        },
        {
            "name": "tranquil_signals",
            "description": "Return active or all Tranquil signals.",
            "inputSchema": {
                "type": "object",
                "properties": {"active": {"type": "boolean"}},
            },
        },
        {
            "name": "tranquil_eval_status",
            "description": "Return recent eval runs and scores.",
            "inputSchema": {
                "type": "object",
                "properties": {"suite": {"type": "string"}},
            },
        },
    ]


def get_run(storage: Storage, run_id: str) -> dict[str, Any]:
    run = storage.get_run(run_id)
    if not run:
        raise KeyError(f"run not found: {run_id}")
    return {
        "run": run,
        "events": storage.get_run_events(run_id),
        "signals": storage.list_run_signals(run_id),
    }


def eval_status(storage: Storage, suite: str | None = None) -> dict[str, Any]:
    data = storage.export_data()
    eval_runs = data["eval_runs"]
    if suite:
        eval_runs = [run for run in eval_runs if run.get("suite") == suite]
    latest = eval_runs[-10:]
    score_by_eval: dict[str, list[dict[str, Any]]] = {}
    for score in data["scores"]:
        score_by_eval.setdefault(score["eval_run_id"], []).append(score)
    return {
        "eval_runs": latest,
        "scores": {run["eval_run_id"]: score_by_eval.get(run["eval_run_id"], []) for run in latest},
    }


def since_from_window(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    try:
        amount = int(text[:-1])
    except ValueError:
        return parse_ts(text)
    unit = text[-1]
    if unit == "m":
        delta = dt.timedelta(minutes=amount)
    elif unit == "h":
        delta = dt.timedelta(hours=amount)
    elif unit == "d":
        delta = dt.timedelta(days=amount)
    else:
        return parse_ts(text)
    return (now_utc() - delta).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def required(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not value:
        raise ValueError(f"missing required argument: {key}")
    return str(value)


def result_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
