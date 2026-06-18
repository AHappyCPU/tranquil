from __future__ import annotations

import os
import json
import shutil
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

from .storage import Storage, extract_paths, extract_tool_command
from .util import (
    command_looks_like_build,
    command_looks_like_check,
    command_looks_like_test,
    json_dumps,
    parse_iso,
    run_user_command,
    safe_int,
    shell_join,
)


SUPPORTED_SCORERS = {
    "no_tool_failures",
    "tests_pass",
    "build_succeeds",
    "diff_applies",
    "no_loops",
    "cost_recorded",
    "checks_if_shipped",
    "cost_budget",
    "latency_budget",
    "no_forbidden_paths",
    "outcome_judge",
}


def run_eval(
    storage: Storage,
    suite: str = "default",
    baseline: str | None = None,
    scorers: list[str] | None = None,
    judge_command: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    fixtures = storage.list_fixtures(suite=suite)
    baseline_eval_run_id = resolve_baseline_eval_run(storage, suite, baseline)
    eval_run_id = storage.create_eval_run(suite=suite, baseline_eval_run_id=baseline_eval_run_id)
    if not fixtures:
        storage.finish_eval_run(eval_run_id, "no_fixtures")
        return eval_run_id, []
    all_passed = True
    for fixture in fixtures:
        run = storage.get_run(fixture["run_id"])
        events = fixture["recorded_trajectory"]
        scores = score_fixture_baseline(fixture, run or {}, events, requested_scorers=scorers, judge_command=judge_command)
        for score in scores:
            all_passed = all_passed and score["passed"]
            storage.add_score(
                eval_run_id,
                fixture["fixture_id"],
                score["scorer"],
                score.get("value"),
                score["passed"],
                score["detail"],
            )
    if baseline_eval_run_id:
        for regression in compare_to_baseline(storage, eval_run_id, baseline_eval_run_id):
            all_passed = False
            storage.add_score(
                eval_run_id,
                regression["fixture_id"],
                regression["scorer"],
                regression.get("value"),
                regression["passed"],
                regression["detail"],
            )
    storage.finish_eval_run(eval_run_id, "passed" if all_passed else "failed")
    return eval_run_id, storage.list_scores(eval_run_id)


def score_fixture_baseline(
    fixture: dict[str, Any],
    run: dict[str, Any],
    events: list[dict[str, Any]],
    requested_scorers: list[str] | None = None,
    judge_command: str | None = None,
) -> list[dict[str, Any]]:
    failures = [event for event in events if event.get("event_type") == "tool_failure"]
    repeated = repeated_tool_inputs(events)
    cost = float(run.get("total_cost_usd_est") or sum((event.get("usage") or {}).get("cost_usd_est") or 0 for event in events))
    produced = bool(run.get("produced_pr"))
    checks = bool(run.get("checks_ran")) or any(event_has_check(event) for event in events)
    repo_ref = fixture.get("repo_ref") if isinstance(fixture.get("repo_ref"), dict) else {}
    budgets = repo_ref.get("budgets") if isinstance(repo_ref.get("budgets"), dict) else {}
    forbidden_paths = repo_ref.get("forbidden_paths") if isinstance(repo_ref.get("forbidden_paths"), list) else []
    scores = [
        {
            "scorer": "no_tool_failures",
            "value": float(len(failures)),
            "passed": len(failures) == 0,
            "detail": {"failures": len(failures)},
        },
        score_command_exit_succeeds("tests_pass", events, command_looks_like_test, "test"),
        score_command_exit_succeeds("build_succeeds", events, command_looks_like_build, "build"),
        score_diff_applies(events),
        {
            "scorer": "no_loops",
            "value": float(max(repeated.values(), default=0)),
            "passed": max(repeated.values(), default=0) < 3,
            "detail": {"repeated_tool_inputs": repeated},
        },
        {
            "scorer": "cost_recorded",
            "value": cost,
            "passed": cost >= 0,
            "detail": {"cost_usd_est": cost},
        },
        {
            "scorer": "checks_if_shipped",
            "value": 1.0 if checks else 0.0,
            "passed": (not produced) or checks,
            "detail": {"produced_pr_or_commit": produced, "checks_ran": checks},
        },
    ]
    requested = set(requested_scorers or [])
    if "cost_usd" in budgets or "cost_budget" in requested:
        budget = float(budgets["cost_usd"]) if "cost_usd" in budgets else None
        scores.append(
            {
                "scorer": "cost_budget",
                "value": cost,
                "passed": budget is None or cost <= budget,
                "detail": {"status": "not_applicable" if budget is None else "scored", "cost_usd_est": cost, "budget_usd": budget},
            }
        )
    if "wall_clock_s" in budgets or "latency_budget" in requested:
        latency = run_wall_clock_s(run)
        budget = float(budgets["wall_clock_s"]) if "wall_clock_s" in budgets else None
        scores.append(
            {
                "scorer": "latency_budget",
                "value": latency,
                "passed": budget is None or (latency is not None and latency <= budget),
                "detail": {"status": "not_applicable" if budget is None else "scored", "wall_clock_s": latency, "budget_s": budget},
            }
        )
    if forbidden_paths or "no_forbidden_paths" in requested:
        touched = sorted({path for event in events for path in extract_paths(event)})
        violations = [path for path in touched if any(fnmatch(path, pattern) for pattern in forbidden_paths)]
        scores.append(
            {
                "scorer": "no_forbidden_paths",
                "value": float(len(violations)),
                "passed": not violations,
                "detail": {
                    "status": "not_applicable" if not forbidden_paths else "scored",
                    "forbidden_paths": forbidden_paths,
                    "violations": violations,
                },
            }
        )
    if "outcome_judge" in requested:
        scores.append(score_outcome_judge(fixture, run, events, judge_command))
    return select_scores(scores, requested_scorers)


def select_scores(scores: list[dict[str, Any]], requested_scorers: list[str] | None) -> list[dict[str, Any]]:
    if not requested_scorers:
        return scores
    requested = [str(scorer) for scorer in requested_scorers]
    by_scorer = {score["scorer"]: score for score in scores}
    selected = [by_scorer[scorer] for scorer in requested if scorer in by_scorer]
    for scorer in requested:
        if scorer not in SUPPORTED_SCORERS:
            selected.append(
                {
                    "scorer": scorer,
                    "value": None,
                    "passed": False,
                    "detail": {"status": "unsupported", "supported_scorers": sorted(SUPPORTED_SCORERS)},
                }
            )
    return selected


def score_command_exit_succeeds(
    scorer: str,
    events: list[dict[str, Any]],
    predicate: Callable[[str], bool],
    command_kind: str,
) -> dict[str, Any]:
    commands = matching_command_events(events, predicate)
    if not commands:
        return {
            "scorer": scorer,
            "value": None,
            "passed": True,
            "detail": {"status": "not_applicable", "reason": f"no_{command_kind}_command_captured", "commands": []},
        }
    failures = [
        command
        for command in commands
        if command["event_type"] == "tool_failure" or (command["exit_code"] is not None and command["exit_code"] != 0)
    ]
    successes = [command for command in commands if command["exit_code"] == 0]
    unknown = [command for command in commands if command["exit_code"] is None and command["event_type"] != "tool_failure"]
    passed = not failures
    status = "passed" if passed else "failed"
    if passed and not successes:
        status = "unknown_exit_codes"
    return {
        "scorer": scorer,
        "value": 1.0 if passed and successes else (0.0 if failures else None),
        "passed": passed,
        "detail": {
            "status": status,
            "commands": commands,
            "failures": failures,
            "unknown_exit_codes": len(unknown),
        },
    }


def matching_command_events(events: list[dict[str, Any]], predicate: Callable[[str], bool]) -> list[dict[str, Any]]:
    matches = []
    for event in events:
        if event.get("event_type") not in {"post_tool", "tool_failure"}:
            continue
        tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
        command = extract_tool_command(tool)
        if not command or not predicate(command):
            continue
        matches.append(
            {
                "event_id": event.get("event_id"),
                "event_type": event.get("event_type"),
                "tool": tool.get("name"),
                "command": command,
                "exit_code": tool_exit_code(event),
            }
        )
    return matches


def score_diff_applies(events: list[dict[str, Any]]) -> dict[str, Any]:
    patch_events = []
    for event in events:
        if event.get("event_type") not in {"post_tool", "tool_failure"}:
            continue
        if not event_looks_like_patch_application(event):
            continue
        tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
        patch_events.append(
            {
                "event_id": event.get("event_id"),
                "event_type": event.get("event_type"),
                "tool": tool.get("name"),
                "command": extract_tool_command(tool),
                "exit_code": tool_exit_code(event),
            }
        )
    if not patch_events:
        return {
            "scorer": "diff_applies",
            "value": None,
            "passed": True,
            "detail": {"status": "not_applicable", "reason": "no_patch_or_edit_event_captured", "patch_events": []},
        }
    failures = [
        event
        for event in patch_events
        if event["event_type"] == "tool_failure" or (event["exit_code"] is not None and event["exit_code"] != 0)
    ]
    unknown = [event for event in patch_events if event["exit_code"] is None and event["event_type"] != "tool_failure"]
    passed = not failures
    status = "passed" if passed else "failed"
    if passed and len(unknown) == len(patch_events):
        status = "unknown_exit_codes"
    return {
        "scorer": "diff_applies",
        "value": 1.0 if passed and len(unknown) < len(patch_events) else (0.0 if failures else None),
        "passed": passed,
        "detail": {
            "status": status,
            "patch_events": patch_events,
            "failures": failures,
            "unknown_exit_codes": len(unknown),
        },
    }


def score_outcome_judge(
    fixture: dict[str, Any],
    run: dict[str, Any],
    events: list[dict[str, Any]],
    judge_command: str | None,
) -> dict[str, Any]:
    repo_ref = fixture.get("repo_ref") if isinstance(fixture.get("repo_ref"), dict) else {}
    if not judge_command:
        return {
            "scorer": "outcome_judge",
            "value": None,
            "passed": False,
            "detail": {
                "status": "unconfigured",
                "reason": "outcome_judge requires judge_command in config.json, TRANQUIL_JUDGE_COMMAND, or --judge-command",
            },
        }
    payload = {
        "fixture_id": fixture.get("fixture_id"),
        "suite": fixture.get("suite"),
        "prompt": fixture.get("prompt"),
        "rubric": repo_ref.get("rubric"),
        "reference": repo_ref.get("reference"),
        "repo_ref": repo_ref,
        "run": run,
        "events": events,
    }
    try:
        completed = run_user_command(
            judge_command,
            input=json_dumps(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "scorer": "outcome_judge",
            "value": None,
            "passed": False,
            "detail": {
                "status": "judge_command_timeout",
                "timeout_s": 120,
                "stdout": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
            },
        }
    if completed.returncode != 0:
        return {
            "scorer": "outcome_judge",
            "value": float(completed.returncode),
            "passed": False,
            "detail": {
                "status": "judge_command_failed",
                "returncode": completed.returncode,
                "stderr": completed.stderr[-2000:],
                "stdout": completed.stdout[-2000:],
            },
        }
    try:
        parsed = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        return {
            "scorer": "outcome_judge",
            "value": None,
            "passed": False,
            "detail": {"status": "invalid_judge_json", "error": str(exc), "stdout": completed.stdout[-2000:]},
        }
    if not isinstance(parsed, dict):
        return {
            "scorer": "outcome_judge",
            "value": None,
            "passed": False,
            "detail": {"status": "invalid_judge_json", "reason": "judge output must be a JSON object"},
        }
    passed = bool(parsed.get("passed"))
    value = parsed.get("score", parsed.get("value"))
    try:
        numeric_value = float(value) if value is not None else (1.0 if passed else 0.0)
    except (TypeError, ValueError):
        numeric_value = 1.0 if passed else 0.0
    detail = dict(parsed)
    detail.setdefault("status", "passed" if passed else "failed")
    detail["judge_command"] = judge_command.split()[0] if judge_command.split() else ""
    return {
        "scorer": "outcome_judge",
        "value": numeric_value,
        "passed": passed,
        "detail": detail,
    }


def event_looks_like_patch_application(event: dict[str, Any]) -> bool:
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
    name = str(tool.get("name") or "").lower()
    if name in {"apply_patch", "edit", "multiedit", "write", "notebookedit", "str_replace_editor"}:
        return True
    command = extract_tool_command(tool)
    if not command:
        return False
    lowered = command.lower()
    return any(term in lowered for term in ["apply_patch", "git apply", "patch -p", "patch <"])


def tool_exit_code(event: dict[str, Any]) -> int | None:
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
    output = tool.get("output")
    if isinstance(output, dict):
        for key in ("exit_code", "returncode", "return_code"):
            value = safe_int(output.get(key))
            if value is not None:
                return value
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    for key in ("exit_code", "returncode", "return_code"):
        value = safe_int(raw.get(key))
        if value is not None:
            return value
    raw_tool = raw.get("tool") if isinstance(raw.get("tool"), dict) else {}
    for key in ("exit_code", "returncode", "return_code"):
        value = safe_int(raw_tool.get(key))
        if value is not None:
            return value
    return None


def resolve_baseline_eval_run(storage: Storage, suite: str, baseline: str | None) -> str | None:
    if not baseline:
        return None
    if baseline == "last-green":
        run = storage.latest_eval_run(suite, status="passed")
        return run["eval_run_id"] if run else None
    return baseline


def compare_to_baseline(storage: Storage, eval_run_id: str, baseline_eval_run_id: str) -> list[dict[str, Any]]:
    baseline_scores = {
        (score["fixture_id"], score["scorer"]): score
        for score in storage.list_scores(baseline_eval_run_id)
        if score["passed"]
    }
    current_scores = {
        (score["fixture_id"], score["scorer"]): score
        for score in storage.list_scores(eval_run_id)
    }
    regressions = []
    for key, baseline_score in baseline_scores.items():
        current = current_scores.get(key)
        if current and not current["passed"]:
            fixture_id, scorer = key
            regressions.append(
                {
                    "fixture_id": fixture_id,
                    "scorer": "regression",
                    "value": 1.0,
                    "passed": False,
                    "detail": {
                        "baseline_eval_run_id": baseline_eval_run_id,
                        "regressed_scorer": scorer,
                        "baseline_value": baseline_score.get("value"),
                        "current_value": current.get("value"),
                    },
                }
            )
    return regressions


def run_wall_clock_s(run: dict[str, Any]) -> float | None:
    started = run.get("started_at")
    ended = run.get("ended_at") or run.get("last_event_at")
    if not started or not ended:
        return None
    return max(0.0, (parse_iso(ended) - parse_iso(started)).total_seconds())


def replay_fixture(
    storage: Storage,
    fixture_id: str,
    command: str | None,
    replay_root: Path,
    agent: str = "command",
    model: str | None = None,
    config_path: str | None = None,
    suite: str | None = None,
    matrix_variant: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    fixture = storage.get_fixture(fixture_id)
    if not fixture:
        raise KeyError(f"fixture not found: {fixture_id}")
    eval_run_id = storage.create_eval_run(suite=suite or fixture["suite"])
    workdir = replay_root.expanduser() / eval_run_id
    workdir.mkdir(parents=True, exist_ok=False)
    (workdir / "prompt.txt").write_text(fixture.get("prompt") or "", encoding="utf-8")
    (workdir / "fixture.json").write_text(json_dumps(fixture), encoding="utf-8")
    repo_dir, materialized = materialize_repo(fixture.get("repo_ref") or {}, workdir)
    storage.add_score(
        eval_run_id,
        fixture_id,
        "repo_materialized",
        1.0 if materialized["passed"] else 0.0,
        materialized["passed"],
        materialized,
    )
    if matrix_variant:
        storage.add_score(
            eval_run_id,
            fixture_id,
            "matrix_variant",
            1.0,
            True,
            {"variant": matrix_variant},
        )
    command = command or replay_command_for_agent(agent=agent, prompt=fixture.get("prompt") or "", model=model)
    if not command:
        storage.finish_eval_run(eval_run_id, "failed")
        raise ValueError("replay requires --command or --agent codex")
    env = os.environ.copy()
    env["TRANQUIL_FIXTURE_ID"] = fixture_id
    env["TRANQUIL_FIXTURE_PROMPT"] = fixture.get("prompt") or ""
    env["TRANQUIL_FIXTURE_FILE"] = str(workdir / "fixture.json")
    env["TRANQUIL_PROMPT_FILE"] = str(workdir / "prompt.txt")
    env["TRANQUIL_REPLAY_DIR"] = str(workdir)
    env["TRANQUIL_REPO_DIR"] = str(repo_dir)
    if model:
        env["TRANQUIL_REPLAY_MODEL"] = model
    if config_path:
        env["TRANQUIL_REPLAY_CONFIG"] = config_path
    completed = run_user_command(
        command,
        cwd=repo_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=None,
    )
    (workdir / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (workdir / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    passed = completed.returncode == 0
    storage.add_score(
        eval_run_id,
        fixture_id,
        "replay_command_exits_zero",
        float(completed.returncode),
        passed,
        {
            "command": command,
            "agent": agent,
            "model": model,
            "config_path": config_path,
            "returncode": completed.returncode,
            "workdir": str(workdir),
            "repo_dir": str(repo_dir),
            "stdout_path": str(workdir / "stdout.txt"),
            "stderr_path": str(workdir / "stderr.txt"),
        },
    )
    storage.finish_eval_run(eval_run_id, "passed" if passed and materialized["passed"] else "failed")
    return eval_run_id, storage.list_scores(eval_run_id)


def run_eval_matrix(
    storage: Storage,
    suite: str,
    matrix: list[dict[str, Any]],
    replay_root: Path,
    default_command: str | None = None,
) -> list[dict[str, Any]]:
    fixtures = storage.list_fixtures(suite=suite)
    results = []
    for index, raw_entry in enumerate(matrix):
        entry = raw_entry if isinstance(raw_entry, dict) else {"name": str(raw_entry)}
        variant = matrix_variant_name(entry, index)
        variant_suite = f"{suite}:{variant}"
        for fixture in fixtures:
            command = entry.get("command") or entry.get("replay_command") or default_command
            agent = str(entry.get("agent") or "command")
            model = str(entry["model"]) if entry.get("model") is not None else None
            config_path = str(entry["config"]) if entry.get("config") is not None else None
            if not command and agent == "command":
                eval_run_id = storage.create_eval_run(suite=variant_suite)
                detail = {
                    "variant": entry,
                    "reason": "matrix entry requires command/replay_command, default replay_command, or agent=codex",
                }
                storage.add_score(eval_run_id, fixture["fixture_id"], "matrix_replay_configured", None, False, detail)
                storage.finish_eval_run(eval_run_id, "failed")
                scores = storage.list_scores(eval_run_id)
            else:
                eval_run_id, scores = replay_fixture(
                    storage,
                    fixture["fixture_id"],
                    command,
                    replay_root,
                    agent=agent,
                    model=model,
                    config_path=config_path,
                    suite=variant_suite,
                    matrix_variant=entry,
                )
            results.append(
                {
                    "variant": variant,
                    "suite": variant_suite,
                    "fixture_id": fixture["fixture_id"],
                    "eval_run_id": eval_run_id,
                    "scores": scores,
                }
            )
    return results


def matrix_variant_name(entry: dict[str, Any], index: int) -> str:
    raw = entry.get("name") or entry.get("id") or entry.get("model") or entry.get("agent") or f"variant-{index + 1}"
    name = "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in str(raw).strip().lower())
    return name.strip("-") or f"variant-{index + 1}"


def materialize_repo(repo_ref: dict[str, Any], workdir: Path) -> tuple[Path, dict[str, Any]]:
    repo_dir = workdir / "repo"
    git_root = repo_ref.get("git_root")
    sha = repo_ref.get("sha")
    if git_root and sha and Path(str(git_root)).exists():
        completed = subprocess.run(
            ["git", "-C", str(git_root), "worktree", "add", "--detach", str(repo_dir), str(sha)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        (workdir / "worktree.stdout.txt").write_text(completed.stdout, encoding="utf-8")
        (workdir / "worktree.stderr.txt").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            repo_dir.mkdir(parents=True, exist_ok=True)
        detail = {
            "passed": completed.returncode == 0,
            "strategy": "git_worktree",
            "git_root": git_root,
            "sha": sha,
            "branch": repo_ref.get("branch"),
            "dirty_at_capture": bool(repo_ref.get("dirty")),
            "returncode": completed.returncode,
            "stdout_path": str(workdir / "worktree.stdout.txt"),
            "stderr_path": str(workdir / "worktree.stderr.txt"),
        }
        dirty_patch = repo_ref.get("dirty_patch")
        if completed.returncode == 0 and isinstance(dirty_patch, str) and dirty_patch:
            patch_path = workdir / "dirty.patch"
            patch_path.write_text(dirty_patch, encoding="utf-8")
            applied = subprocess.run(
                ["git", "-C", str(repo_dir), "apply", "--whitespace=nowarn", str(patch_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            (workdir / "dirty-apply.stdout.txt").write_text(applied.stdout, encoding="utf-8")
            (workdir / "dirty-apply.stderr.txt").write_text(applied.stderr, encoding="utf-8")
            detail.update(
                {
                    "dirty_patch_path": str(patch_path),
                    "dirty_patch_applied": applied.returncode == 0,
                    "dirty_patch_returncode": applied.returncode,
                    "dirty_status": repo_ref.get("dirty_status"),
                    "dirty_apply_stdout_path": str(workdir / "dirty-apply.stdout.txt"),
                    "dirty_apply_stderr_path": str(workdir / "dirty-apply.stderr.txt"),
                }
            )
            detail["passed"] = detail["passed"] and applied.returncode == 0
        return repo_dir, detail
    cwd = repo_ref.get("cwd")
    if cwd and Path(str(cwd)).exists():
        repo_dir.mkdir(parents=True, exist_ok=False)
        return repo_dir, {
            "passed": True,
            "strategy": "empty_replay_dir_with_original_cwd_reference",
            "cwd": cwd,
            "reason": "fixture has no git SHA; original cwd is recorded but not copied",
        }
    repo_dir.mkdir(parents=True, exist_ok=False)
    return repo_dir, {
        "passed": True,
        "strategy": "empty_replay_dir",
        "reason": "fixture has no repo reference",
    }


def replay_command_for_agent(agent: str, prompt: str, model: str | None = None) -> str | None:
    if agent == "codex":
        parts = ["codex", "exec", "--sandbox", "workspace-write"]
        if model:
            parts.extend(["--model", model])
        parts.append(prompt)
        return shell_join(parts)
    return None


def clean_replay_root(replay_root: Path) -> None:
    if replay_root.exists():
        shutil.rmtree(replay_root)


def repeated_tool_inputs(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
        if not tool:
            continue
        key = f"{tool.get('name')}:{tool.get('input')}"
        counts[key] = counts.get(key, 0) + 1
    return {key: value for key, value in counts.items() if value > 1}


def event_has_check(event: dict[str, Any]) -> bool:
    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
    tool_input = tool.get("input")
    command = None
    if isinstance(tool_input, dict):
        command = tool_input.get("command") or tool_input.get("cmd")
    elif isinstance(tool_input, str):
        command = tool_input
    return command_looks_like_check(command) if command else False
