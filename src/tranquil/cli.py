from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .app import run_terminal_app
from .codex_audit import audit_codex_paths
from .config import load_config, save_config
from .evals import replay_fixture, run_eval, run_eval_matrix
from .init import claude_settings_path, codex_hooks_path, run_init
from .mcp import run_mcp_server
from .otel import export_otlp_http
from .signals import scan_idle_runs
from .storage import Storage
from .suites import import_fixture_file, import_suite_fixtures
from .team_sync import push_sync
from .tailer import ingest_path
from .tui import run_tui
from .util import json_dumps


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command_name is None:
        args.command_name = "app"
    try:
        return dispatch(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"tranquil: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tranquil", description="Local eval and observability for coding agents.")
    parser.add_argument("--home", type=Path, default=None, help="Tranquil home directory (default: ~/.tranquil or TRANQUIL_HOME).")
    sub = parser.add_subparsers(dest="command_name")

    app_parser = sub.add_parser("app", help="Start the terminal Fleet view.")
    app_parser.add_argument("--interval", type=float, default=2.0)

    init_parser = sub.add_parser("init", help="Install or remove local agent hooks.")
    init_parser.add_argument("--agent", choices=["all", "claude-code", "codex"], default="all")
    init_parser.add_argument("--scope", choices=["user", "project", "local"], default="user")
    init_parser.add_argument("--undo", action="store_true")
    init_parser.add_argument("--launch", action="store_true", help="Launch the dashboard after wiring hooks.")
    init_parser.add_argument("--no-launch", action="store_true", help="Only wire hooks; do not launch the dashboard.")

    doctor_parser = sub.add_parser("doctor", help="Verify config, SQLite, and hook wiring.")
    doctor_parser.add_argument("--codex-audit", action="store_true", help="Inspect configured Codex rollout paths for local coverage.")

    status_parser = sub.add_parser("status", help="Print fleet status.")
    status_parser.add_argument("--limit", type=int, default=20)
    status_parser.add_argument("--agent", choices=["claude-code", "codex"], default=None)
    status_parser.add_argument("--repo", default=None)
    status_parser.add_argument("--branch", default=None)
    status_parser.add_argument("--label", action="append", default=[], help="Filter by label key or key=value; may be repeated.")
    status_parser.add_argument("--status", default=None, help="Filter by run status.")
    status_parser.add_argument("--line", action="store_true", help="Print one compact status-line summary.")
    status_parser.add_argument("--table", action="store_true", help="Print the expanded run table.")

    signals_parser = sub.add_parser("signals", help="List signals.")
    signals_parser.add_argument("--all", action="store_true", help="Include inactive signals.")

    stop_parser = sub.add_parser("stop", help="Request a local stop for a captured run.")
    stop_parser.add_argument("run_id")
    stop_parser.add_argument("--reason", default="user_requested_stop")

    fixture_parser = sub.add_parser("fixture", help="Manage eval fixtures.")
    fixture_sub = fixture_parser.add_subparsers(dest="fixture_command", required=True)
    fixture_add = fixture_sub.add_parser("add", help="Create a fixture from a run.")
    fixture_add.add_argument("run_id")
    fixture_add.add_argument("--suite", default="default")
    fixture_add.add_argument("--cost-budget", type=float, default=None, help="Estimated cost budget in USD for this fixture.")
    fixture_add.add_argument("--latency-budget", type=float, default=None, help="Wall-clock budget in seconds for this fixture.")
    fixture_add.add_argument("--forbid", action="append", default=[], help="Forbidden touched path glob; may be repeated.")
    fixture_list = fixture_sub.add_parser("list", help="List fixtures.")
    fixture_list.add_argument("--suite", default=None)
    fixture_derive = fixture_sub.add_parser("derive", help="Create fixtures from active signaled runs.")
    fixture_derive.add_argument("--suite", default="signals")
    fixture_sample = fixture_sub.add_parser("sample", help="Sample completed production traces into fixtures.")
    fixture_sample.add_argument("--suite", default="sampled")
    fixture_sample.add_argument("--rate", type=float, default=1.0, help="Deterministic sample rate from 0.0 to 1.0.")
    fixture_sample.add_argument("--limit", type=int, default=20)
    fixture_sample.add_argument("--status", default="completed")
    fixture_sample.add_argument("--agent", choices=["claude-code", "codex"], default=None)
    fixture_sample.add_argument("--repo", default=None)
    fixture_sample.add_argument("--branch", default=None)
    fixture_sample.add_argument("--label", action="append", default=[], help="Filter by label key or key=value; may be repeated.")
    fixture_import = fixture_sub.add_parser("import", help="Import a fixture YAML definition.")
    fixture_import.add_argument("path", type=Path)
    fixture_import.add_argument("--suite", default=None)

    eval_parser = sub.add_parser("eval", help="Run deterministic evals against saved fixtures.")
    eval_parser.add_argument("suite", nargs="?", default="default")
    eval_parser.add_argument("--baseline", default=None, help="Baseline eval run id, or last-green.")
    eval_parser.add_argument("--scorer", action="append", default=[], help="Run only this scorer; may be repeated.")
    eval_parser.add_argument("--judge-command", default=None, help="Command used by outcome_judge; receives JSON on stdin.")

    replay_parser = sub.add_parser("replay", help="Replay a fixture in an isolated directory.")
    replay_parser.add_argument("fixture_id")
    replay_parser.add_argument("--command", "-c", dest="replay_command", default=None, help="Shell command to execute for the replay.")
    replay_parser.add_argument("--agent", choices=["command", "codex"], default="command", help="Replay backend when --command is omitted.")
    replay_parser.add_argument("--model", default=None, help="Model to pass to the agent replay backend.")
    replay_parser.add_argument("--config", dest="replay_config", default=None, help="Replay config path exposed as TRANQUIL_REPLAY_CONFIG.")

    tui_parser = sub.add_parser("tui", help="Terminal fleet and run view.")
    tui_parser.add_argument("--interval", type=float, default=2.0)
    tui_parser.add_argument("--once", action="store_true", help="Render once and exit.")
    tui_parser.add_argument("--run", dest="run_id", default=None, help="Show a run detail view.")
    tui_parser.add_argument("--limit", type=int, default=30)
    tui_parser.add_argument("--agent", choices=["claude-code", "codex"], default=None)
    tui_parser.add_argument("--repo", default=None)
    tui_parser.add_argument("--branch", default=None)
    tui_parser.add_argument("--label", action="append", default=[], help="Filter by label key or key=value; may be repeated.")
    tui_parser.add_argument("--status", default=None)

    ingest_parser = sub.add_parser("ingest", help="Backfill events from Claude JSONL or Codex SQLite files.")
    ingest_parser.add_argument("path", type=Path)
    ingest_parser.add_argument("--agent", choices=["claude-code", "codex"], default=None)
    ingest_parser.add_argument("--limit", type=int, default=None)

    sub.add_parser("mcp", help="Run the Tranquil MCP stdio server.")

    export_parser = sub.add_parser("export", help="Export local Tranquil data.")
    export_parser.add_argument("--json", dest="json_path", type=Path, default=None, help="Write a JSON export to this path.")
    export_parser.add_argument("--otel", dest="otel_endpoint", default=None, help="POST OTLP/HTTP JSON logs to this endpoint.")
    export_parser.add_argument("--header", action="append", default=[], help="HTTP header for --otel, formatted Name=Value.")

    sync_parser = sub.add_parser("sync", help="Push a local export to an opt-in team/cloud endpoint.")
    sync_parser.add_argument("--endpoint", default=None, help="Sync endpoint URL; defaults to sync_endpoint in config.json.")
    sync_parser.add_argument("--header", action="append", default=[], help="HTTP header formatted Name=Value; may be repeated.")

    purge_parser = sub.add_parser("purge", help="Delete old or all local Tranquil data.")
    purge_group = purge_parser.add_mutually_exclusive_group(required=True)
    purge_group.add_argument("--older-than", type=int, metavar="DAYS")
    purge_group.add_argument("--all", action="store_true")

    return parser


def dispatch(args: argparse.Namespace) -> int:
    if args.command_name == "app":
        config = load_config(home=args.home, create=True)
        return run_terminal_app(config, interval=args.interval)
    if args.command_name == "init":
        report = run_init(agent=args.agent, scope=args.scope, undo=args.undo, home=args.home)
        for line in report.lines():
            print(line)
        if should_launch_after_init(args):
            config = load_config(home=args.home, create=True)
            print(f"launching Tranquil terminal app: {config.url}")
            return run_terminal_app(config)
        return 0
    if args.command_name == "doctor":
        return cmd_doctor(args)
    if args.command_name == "status":
        return cmd_status(args)
    if args.command_name == "signals":
        return cmd_signals(args)
    if args.command_name == "stop":
        return cmd_stop(args)
    if args.command_name == "fixture":
        return cmd_fixture(args)
    if args.command_name == "eval":
        return cmd_eval(args)
    if args.command_name == "replay":
        return cmd_replay(args)
    if args.command_name == "tui":
        return cmd_tui(args)
    if args.command_name == "ingest":
        return cmd_ingest(args)
    if args.command_name == "mcp":
        return cmd_mcp(args)
    if args.command_name == "export":
        return cmd_export(args)
    if args.command_name == "sync":
        return cmd_sync(args)
    if args.command_name == "purge":
        return cmd_purge(args)
    raise ValueError(f"unknown command: {args.command_name}")


def should_launch_after_init(args: argparse.Namespace) -> bool:
    if args.undo or args.no_launch:
        return False
    if args.launch:
        return True
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def storage_for(args: argparse.Namespace) -> Storage:
    config = load_config(home=args.home, create=True)
    return Storage(config.db_path, thresholds=config.signal_thresholds, raw_payloads=config.raw_payloads)


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(home=args.home, create=True)
    print(f"home: {config.home}")
    print(f"config: {config.config_path}")
    print(f"db: {config.db_path}")
    for line in doctor_hook_lines(config, Path.cwd()):
        print(line)
    with_storage = Storage(config.db_path, thresholds=config.signal_thresholds, raw_payloads=config.raw_payloads)
    try:
        stats = with_storage.stats()
        print(f"sqlite: ok runs={stats['runs']} signaled={stats['signaled']}")
    finally:
        with_storage.close()
    if args.codex_audit:
        report = audit_codex_paths(config.codex_rollout_paths)
        print("codex audit:")
        print(f"  files: {report['files']} ({report['sqlite_files']} sqlite, {report['json_files']} json)")
        print(f"  event hints: {format_counts(report['event_hints'])}")
        coverage = report["coverage"]
        for key in ("has_prompt", "has_tool", "has_usage", "has_timestamps", "has_sessions"):
            print(f"  {key}: {'yes' if coverage.get(key) else 'no'}")
        if report["errors"]:
            print(f"  errors: {len(report['errors'])}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(home=args.home, create=True)
    storage = Storage(config.db_path, thresholds=config.signal_thresholds, raw_payloads=config.raw_payloads)
    try:
        scan_idle_runs(storage, config.signal_thresholds)
        runs = storage.list_runs(
            limit=args.limit,
            status=args.status,
            agent=args.agent,
            repo=args.repo,
            branch=args.branch,
            labels=args.label,
        )
        if args.line or not args.table:
            stats = storage.stats()
            print(
                f"Tranquil {stats['live']} live / {stats['runs']} runs | "
                f"${stats['cost_usd_est']:.2f} est. | {stats['signaled']} signaled"
            )
            return 0
        if not runs:
            print("No runs captured yet.")
            return 0
        print(f"{'STATE':10} {'AGENT':12} {'COST EST.':>10}  {'REPO / BRANCH':30} RUN")
        for run in runs:
            repo = f"{run.get('repo') or 'unknown'} / {run.get('branch') or '-'}"
            print(f"{run['status'][:10]:10} {run['agent'][:12]:12} ${run['total_cost_usd_est']:>9.2f}  {repo[:30]:30} {run['run_id']}")
        return 0
    finally:
        storage.close()


def cmd_signals(args: argparse.Namespace) -> int:
    config = load_config(home=args.home, create=True)
    storage = Storage(config.db_path, thresholds=config.signal_thresholds, raw_payloads=config.raw_payloads)
    try:
        scan_idle_runs(storage, config.signal_thresholds)
        signals = storage.list_signals(active=None if args.all else True)
        if not signals:
            print("No signals.")
            return 0
        for signal in signals:
            evidence = json.dumps(signal["evidence"], sort_keys=True)
            print(f"{signal['severity']:6} {signal['type']:18} {signal['run_id']} {evidence}")
        return 0
    finally:
        storage.close()


def cmd_stop(args: argparse.Namespace) -> int:
    storage = storage_for(args)
    try:
        inserted = storage.request_stop(args.run_id, reason=args.reason)
        print(f"stop requested: {args.run_id}{'' if inserted else ' (already requested)'}")
        return 0
    finally:
        storage.close()


def cmd_fixture(args: argparse.Namespace) -> int:
    storage = storage_for(args)
    try:
        if args.fixture_command == "add":
            fixture = storage.create_fixture(
                args.run_id,
                suite=args.suite,
                cost_budget_usd=args.cost_budget,
                latency_budget_s=args.latency_budget,
                forbidden_paths=args.forbid,
            )
            print(f"fixture: {fixture['fixture_id']}")
            print(f"suite: {fixture['suite']}")
            return 0
        if args.fixture_command == "list":
            fixtures = storage.list_fixtures(suite=args.suite)
            if not fixtures:
                print("No fixtures.")
                return 0
            for fixture in fixtures:
                print(f"{fixture['fixture_id']} suite={fixture['suite']} run={fixture['run_id']} prompt={short(fixture.get('prompt'))}")
            return 0
        if args.fixture_command == "derive":
            fixtures = storage.create_fixtures_from_signals(suite=args.suite)
            if not fixtures:
                print("No signaled runs need fixtures.")
                return 0
            for fixture in fixtures:
                print(f"fixture: {fixture['fixture_id']} suite={fixture['suite']} run={fixture['run_id']}")
            return 0
        if args.fixture_command == "sample":
            if args.rate < 0 or args.rate > 1:
                print("--rate must be between 0.0 and 1.0", file=sys.stderr)
                return 2
            fixtures = storage.sample_runs(
                suite=args.suite,
                sample_rate=args.rate,
                limit=args.limit,
                status=args.status,
                agent=args.agent,
                repo=args.repo,
                branch=args.branch,
                labels=args.label,
            )
            if not fixtures:
                print("No completed runs sampled.")
                return 0
            for fixture in fixtures:
                print(f"fixture: {fixture['fixture_id']} suite={fixture['suite']} run={fixture['run_id']}")
            return 0
        if args.fixture_command == "import":
            fixture = import_fixture_file(storage, args.path, suite=args.suite)
            print(f"fixture: {fixture['fixture_id']}")
            print(f"suite: {fixture['suite']}")
            return 0
    finally:
        storage.close()
    raise ValueError(f"unknown fixture command: {args.fixture_command}")


def cmd_eval(args: argparse.Namespace) -> int:
    config = load_config(home=args.home, create=True)
    storage = Storage(config.db_path, thresholds=config.signal_thresholds, raw_payloads=config.raw_payloads)
    try:
        suite_name = args.suite
        baseline = args.baseline
        scorers = list(args.scorer or [])
        matrix: list[dict[str, Any]] = []
        if str(args.suite).endswith((".yaml", ".yml")) or Path(args.suite).expanduser().exists():
            suite_def, imported = import_suite_fixtures(storage, args.suite)
            suite_name = str(suite_def["suite"])
            baseline = baseline or suite_def.get("baseline")
            if not scorers:
                scorers = list(suite_def.get("scorers") or [])
            matrix = list(suite_def.get("matrix") or [])
            print(f"suite: {suite_name}")
            print(f"imported fixtures: {len(imported)}")
            if matrix:
                print(f"matrix entries: {len(matrix)}")
            if suite_def.get("scorers"):
                print(f"scorers: {', '.join(suite_def['scorers'])}")
        eval_run_id, scores = run_eval(
            storage,
            suite=suite_name,
            baseline=baseline,
            scorers=scorers or None,
            judge_command=args.judge_command or config.judge_command,
        )
        print(f"eval: {eval_run_id}")
        if not scores:
            print("No fixtures.")
            return 1
        failed = False
        for score in scores:
            mark = "PASS" if score["passed"] else "FAIL"
            failed = failed or not score["passed"]
            print(f"{mark} {score['fixture_id']} {score['scorer']} value={score['value']}")
        if matrix:
            matrix_results = run_eval_matrix(
                storage,
                suite=suite_name,
                matrix=matrix,
                replay_root=config.home / "replays",
                default_command=config.replay_command,
            )
            for result in matrix_results:
                print(f"matrix eval: {result['variant']} fixture={result['fixture_id']} eval={result['eval_run_id']}")
                for score in result["scores"]:
                    mark = "PASS" if score["passed"] else "FAIL"
                    failed = failed or not score["passed"]
                    print(f"{mark} {score['fixture_id']} {score['scorer']} value={score['value']}")
        return 1 if failed else 0
    finally:
        storage.close()


def cmd_replay(args: argparse.Namespace) -> int:
    config = load_config(home=args.home, create=True)
    command = args.replay_command or config.replay_command
    if not command:
        if args.agent == "command":
            print("Replay requires --command, replay_command in config.json, or --agent codex.", file=sys.stderr)
            return 2
    if args.replay_config and not Path(args.replay_config).expanduser().exists():
        print(f"Replay config path does not exist: {args.replay_config}", file=sys.stderr)
        return 2
    storage = Storage(config.db_path, thresholds=config.signal_thresholds, raw_payloads=config.raw_payloads)
    try:
        replay_root = config.home / "replays"
        eval_run_id, scores = replay_fixture(
            storage,
            args.fixture_id,
            command,
            replay_root,
            agent=args.agent,
            model=args.model,
            config_path=args.replay_config,
        )
        print(f"replay eval: {eval_run_id}")
        failed = False
        for score in scores:
            mark = "PASS" if score["passed"] else "FAIL"
            failed = failed or not score["passed"]
            print(f"{mark} {score['scorer']} value={score['value']} detail={json.dumps(score['detail'], sort_keys=True)}")
        return 1 if failed else 0
    finally:
        storage.close()


def cmd_tui(args: argparse.Namespace) -> int:
    config = load_config(home=args.home, create=True)
    storage = Storage(config.db_path, thresholds=config.signal_thresholds, raw_payloads=config.raw_payloads)
    try:
        return run_tui(
            storage,
            config.signal_thresholds,
            interval=args.interval,
            once=args.once,
            run_id=args.run_id,
            limit=args.limit,
            status=args.status,
            agent=args.agent,
            repo=args.repo,
            branch=args.branch,
            labels=args.label,
        )
    finally:
        storage.close()


def cmd_ingest(args: argparse.Namespace) -> int:
    storage = storage_for(args)
    try:
        count = ingest_path(storage, args.path, agent=args.agent, limit=args.limit)
        print(f"ingested: {count}")
        return 0
    finally:
        storage.close()


def cmd_mcp(args: argparse.Namespace) -> int:
    storage = storage_for(args)
    try:
        return run_mcp_server(storage)
    finally:
        storage.close()


def cmd_export(args: argparse.Namespace) -> int:
    storage = storage_for(args)
    try:
        if not args.json_path and not args.otel_endpoint:
            print("export requires --json PATH or --otel ENDPOINT", file=sys.stderr)
            return 2
        if args.json_path:
            data = storage.export_data()
            args.json_path.parent.mkdir(parents=True, exist_ok=True)
            args.json_path.write_text(json_dumps(data) + "\n", encoding="utf-8")
            print(f"exported: {args.json_path}")
        if args.otel_endpoint:
            result = export_otlp_http(storage, args.otel_endpoint, headers=parse_headers(args.header))
            print(f"otel: status={result['status']} records={result['records']}")
        return 0
    finally:
        storage.close()


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config(home=args.home, create=True)
    endpoint = args.endpoint or config.sync_endpoint
    if not endpoint:
        print("sync requires --endpoint or sync_endpoint in config.json", file=sys.stderr)
        return 2
    headers = {**config.sync_headers, **parse_headers(args.header)}
    storage = Storage(config.db_path, thresholds=config.signal_thresholds, raw_payloads=config.raw_payloads)
    try:
        result = push_sync(storage, endpoint, headers=headers)
        print(
            f"sync: status={result['status']} runs={result['runs']} "
            f"events={result['events']} fixtures={result['fixtures']} scores={result['scores']}"
        )
        return 0
    finally:
        storage.close()


def cmd_purge(args: argparse.Namespace) -> int:
    storage = storage_for(args)
    try:
        counts = storage.purge(older_than_days=args.older_than, all_data=args.all)
        for table, count in counts.items():
            print(f"{table}: {count}")
        return 0
    finally:
        storage.close()


def short(value: Any, length: int = 72) -> str:
    text = str(value or "").replace("\n", " ")
    return text if len(text) <= length else text[: length - 1] + "..."


def parse_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"header must be Name=Value: {value}")
        key, header_value = value.split("=", 1)
        if not key:
            raise ValueError("header name cannot be empty")
        headers[key] = header_value
    return headers


def format_counts(values: dict[str, int], limit: int = 6) -> str:
    if not values:
        return "none"
    ordered = sorted(values.items(), key=lambda item: (-int(item[1]), item[0]))
    return ", ".join(f"{key}={count}" for key, count in ordered[:limit])


def doctor_hook_lines(config: Any, cwd: Path) -> list[str]:
    checks = [
        ("claude user hooks", claude_settings_path("user")),
        ("claude project hooks", claude_settings_path("project", cwd=cwd)),
        ("codex user hooks", codex_hooks_path("user")),
        ("codex project hooks", codex_hooks_path("project", cwd=cwd)),
    ]
    lines = []
    for label, path in checks:
        if not path.exists():
            lines.append(f"{label}: missing ({path})")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            lines.append(f"{label}: invalid JSON ({path})")
            continue
        if has_tranquil_command_hook(payload):
            lines.append(f"{label}: ok ({path})")
        elif has_stale_http_hook(payload):
            lines.append(f"{label}: stale HTTP hooks; run 'tranquil init' to upgrade ({path})")
        else:
            lines.append(f"{label}: missing Tranquil hook ({path})")
    return lines


def has_tranquil_command_hook(payload: dict[str, Any]) -> bool:
    for hook in iter_hooks(payload):
        command = str(hook.get("command", ""))
        if "tranquil" in command and ("hook_forwarder" in command or "hook-forward" in command):
            return True
    return False


def has_stale_http_hook(payload: dict[str, Any]) -> bool:
    for hook in iter_hooks(payload):
        if "/hooks/" in str(hook.get("url", "")):
            return True
    return False


def iter_hooks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    hooks = payload.get("hooks")
    found: list[dict[str, Any]] = []
    if not isinstance(hooks, dict):
        return found
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
                if isinstance(hook, dict):
                    found.append(hook)
    return found


if __name__ == "__main__":
    raise SystemExit(main())
