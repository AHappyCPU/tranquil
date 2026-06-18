from __future__ import annotations

import contextlib
import io
import os
import sys
import time
from typing import Any

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import SignalThresholds
from .signals import scan_idle_runs
from .storage import Storage

if os.name == "nt":
    import msvcrt
else:
    import select
    import termios
    import tty


DEFAULT_WIDTH = 128


def render_fleet(
    storage: Storage,
    thresholds: SignalThresholds,
    limit: int = 30,
    status: str | None = None,
    agent: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    labels: list[str] | None = None,
    selected_index: int = 0,
    width: int = DEFAULT_WIDTH,
) -> str:
    screen = build_fleet_screen(
        storage,
        thresholds,
        limit=limit,
        status=status,
        agent=agent,
        repo=repo,
        branch=branch,
        labels=labels,
        selected_index=selected_index,
    )
    return capture(screen, width=width)


def render_run(storage: Storage, thresholds: SignalThresholds, run_id: str, events_limit: int = 12, width: int = DEFAULT_WIDTH) -> str:
    return capture(build_run_screen(storage, thresholds, run_id, events_limit=events_limit), width=width)


def build_fleet_screen(
    storage: Storage,
    thresholds: SignalThresholds,
    limit: int = 30,
    status: str | None = None,
    agent: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    labels: list[str] | None = None,
    selected_index: int = 0,
) -> Group:
    scan_idle_runs(storage, thresholds)
    stats = storage.stats()
    runs = fleet_runs(storage, limit=limit, status=status, agent=agent, repo=repo, branch=branch, labels=labels)
    selected_index = clamp_index(selected_index, len(runs))
    active_signals = storage.list_signals(active=True, limit=100)
    signals_by_run = group_signals_by_run(active_signals)
    eval_runs = storage.list_eval_runs(limit=5)

    title = Text.assemble(
        ("TRANQUIL Fleet", "bold"),
        ("  "),
        (f"{stats['live']} live", "green"),
        (" / "),
        (f"{stats['runs']} runs", "cyan"),
        (" / "),
        (f"${stats['cost_usd_est']:.2f} est.", "yellow"),
        (" / "),
        (f"{stats['signaled']} signaled", "red" if stats["signaled"] else "green"),
    )
    table = Table(box=box.SIMPLE_HEAVY, expand=True, pad_edge=False)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("#", justify="right", width=3)
    table.add_column("Ag", width=4)
    table.add_column("State", width=12)
    table.add_column("Cost", justify="right", width=9)
    table.add_column("Subs", justify="right", width=5)
    table.add_column("Repo / Branch", ratio=2, min_width=20)
    table.add_column("Activity", width=12)
    table.add_column("Run", width=14)
    table.add_column("Note", ratio=2, min_width=18)
    if not runs:
        table.add_row("", "-", "-", "no runs", "-", "-", "Waiting for captured events", "", "", "")
    for index, run in enumerate(runs, start=1):
        run_signals = signals_by_run.get(run["run_id"], [])
        selected = index - 1 == selected_index
        marker = Text(">", style="bold reverse") if selected else Text("")
        repo_branch = f"{run.get('repo') or 'unknown'} / {run.get('branch') or '-'}{scheduled_suffix(run)}"
        row_style = "reverse" if selected else None
        table.add_row(
            marker,
            str(index),
            agent_badge(run.get("agent")),
            state_text(run, run_signals),
            money(run.get("total_cost_usd_est")),
            str(int(run.get("subagents_count") or 0)),
            Text(repo_branch, style="bold" if selected else ""),
            spark(run.get("activity") or []),
            short(run["run_id"], 14),
            run_note(run, run_signals),
            style=row_style,
        )

    side_panels = [build_signals_panel(active_signals), build_evals_panel(eval_runs)]
    if runs:
        side_panels.insert(0, build_peek_panel(runs[selected_index], signals_by_run.get(runs[selected_index]["run_id"], [])))
    footer = Text("Keys: n/j/down next | p/up prev | enter/o/right open | b/left fleet | k stop | r refresh | q quit", style="dim")
    return Group(
        Panel(Align.left(title), box=box.ROUNDED, padding=(0, 1)),
        table,
        Columns(side_panels, equal=True, expand=True),
        Panel(footer, box=box.ROUNDED, padding=(0, 1)),
    )


def build_run_screen(storage: Storage, thresholds: SignalThresholds, run_id: str, events_limit: int = 12) -> Group | Panel:
    scan_idle_runs(storage, thresholds)
    run = storage.get_run(run_id)
    if not run:
        return Panel(f"TRANQUIL Run | not found: {run_id}", title="TRANQUIL Run", border_style="red")
    signals = storage.list_run_signals(run_id)
    subagents = storage.list_subagents(run_id)
    files = storage.file_touch_summary(run_id)
    scores = storage.list_run_scores(run_id)
    events = storage.get_recent_events(run_id, limit=events_limit)

    summary = Table.grid(expand=True)
    summary.add_column(ratio=2)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_row(
        Text.assemble(("State ", "dim"), state_text(run, signals), ("  "), (agent_label(run.get("agent")), "bold cyan")),
        Text.assemble(("Cost ", "dim"), (f"{money(run.get('total_cost_usd_est'))} est.", "yellow")),
        Text.assemble(("Run ", "dim"), (short(run_id, 22), "cyan")),
    )
    summary.add_row(
        Text(f"{run.get('repo') or 'unknown'} / {run.get('branch') or '-'}{scheduled_suffix(run)}", style="bold"),
        f"tools {run.get('tool_calls') or 0}",
        f"files {run.get('files_touched') or 0}",
    )
    summary.add_row(
        f"signals {run.get('signals_count') or 0}",
        f"subagents {run.get('subagents_count') or 0}",
        f"depth {run.get('max_depth') or 0}",
    )

    narrative: list[Any] = []
    if run.get("first_prompt"):
        narrative.append(Text.assemble(("Prompt: ", "bold"), short(run["first_prompt"], 140)))
    if run.get("latest_message"):
        narrative.append(Text.assemble(("Latest: ", "bold"), short(run["latest_message"], 140)))
    if not narrative:
        narrative.append(Text("No prompt or assistant message captured yet.", style="dim"))

    footer = Text("Keys: b/left fleet | k stop | r refresh | q quit", style="dim")
    return Group(
        Panel(summary, title=f"TRANQUIL Run | {run_id}", box=box.ROUNDED, padding=(0, 1)),
        Panel(Group(*narrative), title="Context", box=box.ROUNDED, padding=(0, 1)),
        Columns(
            [
                build_signals_panel(signals, title="Signals"),
                build_subagents_panel(subagents),
                build_scores_panel(scores),
            ],
            equal=True,
            expand=True,
        ),
        Columns([build_files_panel(files), build_events_panel(events)], equal=True, expand=True),
        Panel(footer, box=box.ROUNDED, padding=(0, 1)),
    )


def build_peek_panel(run: dict[str, Any], signals: list[dict[str, Any]]) -> Panel:
    lines = [
        Text.assemble((run["run_id"], "bold cyan"), ("  "), state_text(run, signals)),
        Text(f"tools {run.get('tool_calls') or 0} / files {run.get('files_touched') or 0} / signals {run.get('signals_count') or 0}", style="dim"),
    ]
    if run.get("first_prompt"):
        lines.append(Text.assemble(("prompt: ", "bold"), short(run["first_prompt"], 92)))
    if run.get("latest_message"):
        lines.append(Text.assemble(("latest: ", "bold"), short(run["latest_message"], 92)))
    if signals:
        lines.append(Text("active: " + ", ".join(short(signal["type"], 18) for signal in signals[:4]), style="red"))
    return Panel(Group(*lines), title="Peek", box=box.ROUNDED, padding=(0, 1))


def build_signals_panel(signals: list[dict[str, Any]], title: str = "Active Signals") -> Panel:
    table = Table(box=None, expand=True, pad_edge=False)
    table.add_column("Severity", width=8)
    table.add_column("Type", ratio=1)
    table.add_column("Reason", ratio=2)
    if not signals:
        table.add_row("-", "none", "")
    for signal in signals[:8]:
        evidence = signal.get("evidence") or {}
        reason = evidence.get("reason") or evidence.get("path") or evidence.get("tool") or evidence.get("message") or ""
        table.add_row(
            Text(str(signal["severity"]), style=severity_style(signal.get("severity"))),
            short(signal.get("type"), 22),
            short(reason, 48),
        )
    return Panel(table, title=title, box=box.ROUNDED, padding=(0, 1))


def build_evals_panel(eval_runs: list[dict[str, Any]]) -> Panel:
    table = Table(box=None, expand=True, pad_edge=False)
    table.add_column("Suite", ratio=2)
    table.add_column("Status", width=10)
    table.add_column("Scores", justify="right", width=12)
    if not eval_runs:
        table.add_row("none", "-", "-")
    for eval_run in eval_runs:
        table.add_row(
            short(eval_run["suite"], 24),
            Text(str(eval_run["status"]), style="green" if eval_run["status"] == "passed" else "red" if eval_run["status"] == "failed" else "yellow"),
            f"{eval_run.get('passed_count', 0)} / {eval_run.get('failed_count', 0)}",
        )
    return Panel(table, title="Recent Evals", box=box.ROUNDED, padding=(0, 1))


def build_subagents_panel(subagents: list[dict[str, Any]]) -> Panel:
    table = Table(box=None, expand=True, pad_edge=False)
    table.add_column("Depth", justify="right", width=5)
    table.add_column("Session", ratio=2)
    table.add_column("State", width=10)
    table.add_column("Cost", justify="right", width=8)
    if not subagents:
        table.add_row("-", "none", "-", "-")
    for subagent in subagents[:10]:
        table.add_row(
            str(subagent.get("depth") or 0),
            short(subagent.get("session_id"), 24),
            short(subagent.get("status"), 10),
            money(subagent.get("cost_usd_est")),
        )
    return Panel(table, title="Subagents", box=box.ROUNDED, padding=(0, 1))


def build_files_panel(files: list[dict[str, Any]]) -> Panel:
    table = Table(box=None, expand=True, pad_edge=False)
    table.add_column("Path", ratio=3)
    table.add_column("R", justify="right", width=4)
    table.add_column("W", justify="right", width=4)
    table.add_column("Tools", ratio=2)
    if not files:
        table.add_row("none", "-", "-", "")
    for item in files[:12]:
        path = Text(short(item["path"], 48), style="red" if item.get("reread_thrash") else "")
        table.add_row(
            path,
            str(item.get("reads") or 0),
            str(item.get("writes") or 0),
            short(",".join(item.get("tools") or []), 24),
        )
    return Panel(table, title="Files", box=box.ROUNDED, padding=(0, 1))


def build_scores_panel(scores: list[dict[str, Any]]) -> Panel:
    table = Table(box=None, expand=True, pad_edge=False)
    table.add_column("Scorer", ratio=2)
    table.add_column("Result", width=8)
    table.add_column("Value", justify="right", width=10)
    if not scores:
        table.add_row("none", "-", "-")
    for score in scores[:10]:
        table.add_row(
            short(score["scorer"], 24),
            Text("pass" if score["passed"] else "fail", style="green" if score["passed"] else "red"),
            str(score.get("value")),
        )
    return Panel(table, title="Eval Scores", box=box.ROUNDED, padding=(0, 1))


def build_events_panel(events: list[dict[str, Any]]) -> Panel:
    table = Table(box=None, expand=True, pad_edge=False)
    table.add_column("Type", width=16)
    table.add_column("Detail", ratio=3)
    table.add_column("Cost", justify="right", width=9)
    if not events:
        table.add_row("none", "", "")
    for event in reversed(events):
        tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        command = tool_command(tool)
        detail = tool.get("name") or event.get("message") or command or ""
        table.add_row(
            short(event.get("event_type"), 16),
            short(detail, 72),
            money(usage.get("cost_usd_est")) if usage.get("cost_usd_est") else "",
        )
    return Panel(table, title="Recent Events", box=box.ROUNDED, padding=(0, 1))


def run_tui(
    storage: Storage,
    thresholds: SignalThresholds,
    interval: float = 2.0,
    once: bool = False,
    run_id: str | None = None,
    limit: int = 30,
    status: str | None = None,
    agent: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    labels: list[str] | None = None,
    stdin: Any | None = None,
    stdout: Any | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    selected_index = 0
    current_run_id = run_id
    interactive = bool(
        not once
        and getattr(stdin, "isatty", lambda: False)()
        and getattr(stdout, "isatty", lambda: False)()
    )
    if once:
        text = (
            render_run(storage, thresholds, current_run_id)
            if current_run_id
            else render_fleet(
                storage,
                thresholds,
                limit=limit,
                status=status,
                agent=agent,
                repo=repo,
                branch=branch,
                labels=labels,
                selected_index=selected_index,
            )
        )
        stdout.write(text + "\n")
        stdout.flush()
        return 0
    if not interactive:
        while True:
            text = (
                render_run(storage, thresholds, current_run_id)
                if current_run_id
                else render_fleet(
                    storage,
                    thresholds,
                    limit=limit,
                    status=status,
                    agent=agent,
                    repo=repo,
                    branch=branch,
                    labels=labels,
                    selected_index=selected_index,
                )
            )
            stdout.write(text + "\n")
            stdout.flush()
            time.sleep(interval)
    console = Console(file=stdout)
    with raw_terminal(stdin), Live(
        current_screen(
            storage,
            thresholds,
            current_run_id,
            selected_index,
            limit=limit,
            status=status,
            agent=agent,
            repo=repo,
            branch=branch,
            labels=labels,
        ),
        console=console,
        screen=True,
        refresh_per_second=8,
        transient=False,
    ) as live:
        while True:
            live.update(
                current_screen(
                    storage,
                    thresholds,
                    current_run_id,
                    selected_index,
                    limit=limit,
                    status=status,
                    agent=agent,
                    repo=repo,
                    branch=branch,
                    labels=labels,
                ),
                refresh=True,
            )
            key = read_key(stdin, interval)
            if not key or key == "r":
                continue
            if key == "q":
                return 0
            if key in {"b", "left"}:
                current_run_id = None
                continue
            if key == "k":
                target = current_run_id or selected_run_id(
                    storage,
                    selected_index,
                    limit=limit,
                    status=status,
                    agent=agent,
                    repo=repo,
                    branch=branch,
                    labels=labels,
                )
                if target:
                    storage.request_stop(target)
                continue
            if current_run_id:
                continue
            run_count = len(fleet_runs(storage, limit=limit, status=status, agent=agent, repo=repo, branch=branch, labels=labels))
            if key in {"n", "j", "down"}:
                selected_index = clamp_index(selected_index + 1, run_count)
                continue
            if key in {"p", "up"}:
                selected_index = clamp_index(selected_index - 1, run_count)
                continue
            if key in {"enter", "o", "right"}:
                selected = selected_run_id(
                    storage,
                    selected_index,
                    limit=limit,
                    status=status,
                    agent=agent,
                    repo=repo,
                    branch=branch,
                    labels=labels,
                )
                if selected:
                    current_run_id = selected
                continue


def current_screen(
    storage: Storage,
    thresholds: SignalThresholds,
    run_id: str | None,
    selected_index: int,
    limit: int,
    status: str | None,
    agent: str | None,
    repo: str | None,
    branch: str | None,
    labels: list[str] | None,
) -> Group | Panel:
    if run_id:
        return build_run_screen(storage, thresholds, run_id)
    return build_fleet_screen(
        storage,
        thresholds,
        limit=limit,
        status=status,
        agent=agent,
        repo=repo,
        branch=branch,
        labels=labels,
        selected_index=selected_index,
    )


def capture(renderable: Any, width: int = DEFAULT_WIDTH) -> str:
    output = io.StringIO()
    console = Console(file=output, width=width, force_terminal=False, color_system=None, legacy_windows=False)
    console.print(renderable)
    return output.getvalue().rstrip()


def fleet_runs(
    storage: Storage,
    limit: int,
    status: str | None,
    agent: str | None,
    repo: str | None,
    branch: str | None,
    labels: list[str] | None,
) -> list[dict[str, Any]]:
    return storage.list_runs(limit=limit, status=status, agent=agent, repo=repo, branch=branch, labels=labels)


def selected_run_id(
    storage: Storage,
    selected_index: int,
    limit: int,
    status: str | None,
    agent: str | None,
    repo: str | None,
    branch: str | None,
    labels: list[str] | None,
) -> str | None:
    runs = fleet_runs(storage, limit=limit, status=status, agent=agent, repo=repo, branch=branch, labels=labels)
    if not runs:
        return None
    return runs[clamp_index(selected_index, len(runs))]["run_id"]


@contextlib.contextmanager
def raw_terminal(stdin: Any) -> Any:
    if os.name == "nt":
        yield
        return
    fd = stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def read_key(stdin: Any, timeout: float) -> str:
    if os.name == "nt":
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not msvcrt.kbhit():
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                continue
            first = msvcrt.getwch()
            if first in {"\r", "\n"}:
                return "enter"
            if first in {"\x00", "\xe0"}:
                second = msvcrt.getwch()
                return {"H": "up", "P": "down", "M": "right", "K": "left"}.get(second, "")
            return first
        return ""
    ready, _, _ = select.select([stdin], [], [], timeout)
    if not ready:
        return ""
    first = stdin.read(1)
    if first in {"\r", "\n"}:
        return "enter"
    if first == "\x1b":
        ready, _, _ = select.select([stdin], [], [], 0.01)
        if not ready:
            return "escape"
        rest = stdin.read(2)
        return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(rest, "escape")
    return first


def group_signals_by_run(signals: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        grouped.setdefault(signal["run_id"], []).append(signal)
    return grouped


def state_text(run: dict[str, Any], signals: list[dict[str, Any]] | None = None) -> Text:
    state = display_state(run, signals)
    return Text(state, style=state_style(state))


def display_state(run: dict[str, Any], signals: list[dict[str, Any]] | None = None) -> str:
    signal_types = {signal.get("type") for signal in signals or []}
    if "stop_requested" in signal_types:
        return "stop req"
    if "scheduled_idle" in signal_types:
        return "sched idle"
    if "loop" in signal_types:
        return "looping"
    if "runaway_cost" in signal_types:
        return "cost high"
    if "failure_cascade" in signal_types:
        return "failing"
    if "stuck_idle" in signal_types:
        return "idle"
    if is_scheduled(run) and run.get("status") in {"running", "waiting"}:
        return "scheduled"
    return str(run.get("status") or "unknown")


def state_style(state: str) -> str:
    if state in {"completed", "passed"}:
        return "green"
    if state in {"running", "scheduled"}:
        return "cyan"
    if state in {"waiting", "idle", "sched idle"}:
        return "yellow"
    if state in {"failed", "looping", "cost high", "failing", "stop req"}:
        return "red bold"
    return "white"


def severity_style(severity: Any) -> str:
    value = str(severity or "").lower()
    if value == "high":
        return "red bold"
    if value == "medium":
        return "yellow"
    if value == "low":
        return "cyan"
    return "white"


def is_scheduled(run: dict[str, Any]) -> bool:
    labels = run.get("labels") if isinstance(run.get("labels"), dict) else {}
    values = {str(value).lower() for items in labels.values() for value in (items if isinstance(items, list) else [items])}
    keys = {str(key).lower() for key in labels}
    return bool(
        {"scheduled", "schedule", "background", "nightly"} & values
        or {"schedule", "scheduled", "background"} & keys
        or "kind" in keys and {"scheduled", "background", "nightly"} & values
    )


def scheduled_suffix(run: dict[str, Any]) -> str:
    return " [sched]" if is_scheduled(run) else ""


def clamp_index(index: int, count: int) -> int:
    if count <= 0:
        return 0
    return max(0, min(index, count - 1))


def agent_label(agent: Any) -> str:
    return "CX" if agent == "codex" else "CC"


def agent_badge(agent: Any) -> Text:
    label = agent_label(agent)
    style = "bold black on bright_blue" if label == "CX" else "bold black on bright_green"
    return Text(f" {label} ", style=style)


def money(value: Any) -> str:
    try:
        return f"${float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def spark(values: list[int]) -> Text:
    if not values:
        return Text("............", style="dim")
    bars = [int(value or 0) for value in values[:12]]
    bars.extend([0] * (12 - len(bars)))
    high = max(bars) or 1
    text = "".join("#" if value >= high * 0.66 else "+" if value else "." for value in bars)
    return Text(text, style="green" if any(bars) else "dim")


def run_note(run: dict[str, Any], signals: list[dict[str, Any]] | None = None) -> str:
    notes = []
    signal_types = {signal.get("type") for signal in signals or []}
    if "stop_requested" in signal_types:
        notes.append("stop requested")
    if is_scheduled(run):
        notes.append("scheduled")
    if run.get("signals_count"):
        notes.append(f"{run['signals_count']} signals")
    if run.get("produced_pr"):
        notes.append("shipped")
    if run.get("checks_ran"):
        notes.append("checked")
    if run.get("latest_message"):
        notes.append(str(run["latest_message"]))
    labels = run.get("labels") if isinstance(run.get("labels"), dict) else {}
    for key, values in labels.items():
        value = values[0] if isinstance(values, list) and values else ""
        notes.append(f"{key}={value}" if value else str(key))
        if len(notes) >= 3:
            break
    return " | ".join(notes) if notes else "-"


def tool_command(tool: dict[str, Any]) -> str | None:
    raw_input = tool.get("input")
    if isinstance(raw_input, dict):
        value = raw_input.get("command") or raw_input.get("cmd") or raw_input.get("script")
        return str(value) if value else None
    if isinstance(raw_input, str):
        return raw_input
    return None


def short(value: Any, length: int) -> str:
    text = str(value or "").replace("\n", " ")
    if len(text) <= length:
        return text
    return text[: max(0, length - 1)] + "."
