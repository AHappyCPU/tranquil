from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from .config import SignalThresholds
from .storage import extract_tool_command, extract_paths
from .util import command_looks_like_check, compact_json_for_fingerprint, parse_iso

if TYPE_CHECKING:
    from .storage import Storage


def evaluate_run_signals(storage: "Storage", run_id: str, thresholds: SignalThresholds) -> None:
    events = storage.get_run_events(run_id)
    if not events:
        return
    detect_loop(storage, run_id, events, thresholds)
    detect_runaway_cost(storage, run_id, thresholds)
    detect_skipped_checks(storage, run_id, events)
    detect_reread_thrash(storage, run_id, events, thresholds)
    detect_failure_cascade(storage, run_id, events, thresholds)


def scan_idle_runs(storage: "Storage", thresholds: SignalThresholds) -> None:
    from .util import now_utc

    for run in storage.list_runs(limit=500):
        if run["status"] not in {"running", "waiting"}:
            continue
        age_min = (now_utc() - parse_iso(run["last_event_at"])).total_seconds() / 60
        if run_is_scheduled(run) and age_min >= thresholds.scheduled_idle_minutes:
            storage.add_signal(
                run["run_id"],
                "scheduled_idle",
                "high",
                {
                    "reason": "scheduled_or_background_run_idle",
                    "idle_minutes": round(age_min, 1),
                    "threshold_minutes": thresholds.scheduled_idle_minutes,
                    "labels": run.get("labels") or {},
                    "fingerprint": "scheduled_idle",
                },
            )
        if age_min >= thresholds.idle_minutes:
            storage.add_signal(
                run["run_id"],
                "stuck_idle",
                "medium",
                {"reason": "no_recent_events", "idle_minutes": round(age_min, 1), "fingerprint": "idle"},
            )


def run_is_scheduled(run: dict[str, Any]) -> bool:
    labels = run.get("labels") if isinstance(run.get("labels"), dict) else {}
    marker_keys = {"scheduled", "schedule", "background", "bg", "cron", "nightly"}
    marker_values = {"scheduled", "schedule", "background", "bg", "cron", "nightly"}
    for raw_key, raw_values in labels.items():
        key = str(raw_key).lower()
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        if key in marker_keys:
            if not values:
                return True
            for value in values:
                lowered = str(value).lower()
                if lowered not in {"false", "no", "0", "manual"}:
                    return True
        for value in values:
            if str(value).lower() in marker_values:
                return True
    return False


def detect_loop(storage: "Storage", run_id: str, events: list[dict[str, Any]], thresholds: SignalThresholds) -> None:
    fingerprints: Counter[str] = Counter()
    evidence: dict[str, Any] = {}
    for event in events:
        if event.get("event_type") not in {"post_tool", "tool_failure", "pre_tool"}:
            continue
        tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
        name = tool.get("name") or "unknown"
        fingerprint = f"{name}:{compact_json_for_fingerprint(tool.get('input'))}"
        fingerprints[fingerprint] += 1
        evidence[fingerprint] = {"tool": name, "input": tool.get("input"), "fingerprint": fingerprint}
    for fingerprint, count in fingerprints.items():
        if count >= thresholds.loop_repeats:
            detail = dict(evidence[fingerprint])
            detail["count"] = count
            storage.add_signal(run_id, "loop", "high", detail)


def detect_runaway_cost(storage: "Storage", run_id: str, thresholds: SignalThresholds) -> None:
    run = storage.get_run(run_id)
    if not run:
        return
    total = float(run.get("total_cost_usd_est") or 0)
    if total >= thresholds.runaway_cost_usd:
        storage.add_signal(
            run_id,
            "runaway_cost",
            "high",
            {"reason": "run_cost_over_budget", "cost_usd_est": total, "budget_usd": thresholds.runaway_cost_usd, "fingerprint": "total"},
        )
        return
    try:
        started = parse_iso(run["started_at"])
        last = parse_iso(run["last_event_at"])
    except (KeyError, ValueError):
        return
    minutes = max((last - started).total_seconds() / 60, 0.1)
    rate = total / minutes
    if rate >= thresholds.runaway_cost_per_min_usd:
        storage.add_signal(
            run_id,
            "runaway_cost",
            "high",
            {
                "reason": "cost_rate_over_budget",
                "cost_per_min_usd_est": round(rate, 4),
                "budget_per_min_usd": thresholds.runaway_cost_per_min_usd,
                "fingerprint": "rate",
            },
        )


def detect_skipped_checks(storage: "Storage", run_id: str, events: list[dict[str, Any]]) -> None:
    run = storage.get_run(run_id)
    if not run or run["status"] != "completed" or not run["produced_pr"] or run["checks_ran"]:
        return
    ran_check = False
    for event in events:
        tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
        command = extract_tool_command(tool)
        if command and command_looks_like_check(command):
            ran_check = True
            break
    if not ran_check:
        storage.add_signal(
            run_id,
            "skipped_checks",
            "medium",
            {"reason": "produced_pr_or_commit_without_test_or_build", "fingerprint": "default"},
        )


def detect_reread_thrash(storage: "Storage", run_id: str, events: list[dict[str, Any]], thresholds: SignalThresholds) -> None:
    reads: Counter[str] = Counter()
    for event in events:
        tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
        if str(tool.get("name") or "").lower() != "read":
            continue
        for path in extract_paths(event):
            reads[path] += 1
    for path, count in reads.items():
        if count >= thresholds.reread_repeats:
            storage.add_signal(
                run_id,
                "reread_thrash",
                "low",
                {"path": path, "read_count": count, "threshold": thresholds.reread_repeats},
            )


def detect_failure_cascade(storage: "Storage", run_id: str, events: list[dict[str, Any]], thresholds: SignalThresholds) -> None:
    recent = events[-10:]
    failures = [event for event in recent if event.get("event_type") == "tool_failure"]
    if len(failures) >= thresholds.failure_cascade_count:
        storage.add_signal(
            run_id,
            "failure_cascade",
            "high",
            {
                "failures_in_recent_events": len(failures),
                "window": len(recent),
                "fingerprint": "recent_failures",
            },
        )
