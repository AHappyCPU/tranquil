from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


@dataclass(slots=True)
class SignalThresholds:
    loop_repeats: int = 3
    reread_repeats: int = 5
    failure_cascade_count: int = 3
    runaway_cost_usd: float = 5.0
    runaway_cost_per_min_usd: float = 2.0
    idle_minutes: int = 20
    scheduled_idle_minutes: int = 10


@dataclass(slots=True)
class TranquilConfig:
    home: Path
    db_path: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str = ""
    retention_days: int = 30
    raw_payloads: bool = True
    signal_thresholds: SignalThresholds = field(default_factory=SignalThresholds)
    transcript_paths: list[str] = field(default_factory=list)
    codex_rollout_paths: list[str] = field(default_factory=list)
    tail_interval_seconds: float = 2.0
    kill_switch_enabled: bool = False
    run_cost_budget_usd: float = 10.0
    replay_command: str | None = None
    editor_command: str | None = None
    judge_command: str | None = None
    notification_webhook_url: str | None = None
    notification_command: str | None = None
    policy_enabled: bool = False
    policy_forbidden_paths: list[str] = field(default_factory=list)
    policy_forbidden_commands: list[str] = field(default_factory=list)
    trace_sampling_enabled: bool = False
    trace_sample_rate: float = 0.05
    trace_sample_suite: str = "sampled"
    sync_endpoint: str | None = None
    sync_headers: dict[str, str] = field(default_factory=dict)

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def default_home() -> Path:
    return Path(os.environ.get("TRANQUIL_HOME", "~/.tranquil")).expanduser()


def default_config(home: Path | None = None) -> TranquilConfig:
    root = (home or default_home()).expanduser()
    token = os.environ.get("TRANQUIL_TOKEN") or secrets.token_urlsafe(32)
    codex_paths = [
        str(Path("~/.codex").expanduser()),
        str(Path("~/.codex/sessions").expanduser()),
    ]
    transcript_paths = [
        str(Path("~/.claude/projects").expanduser()),
    ]
    return TranquilConfig(
        home=root,
        db_path=root / "tranquil.db",
        token=token,
        transcript_paths=transcript_paths,
        codex_rollout_paths=codex_paths,
        editor_command=os.environ.get("TRANQUIL_EDITOR_COMMAND") or None,
        judge_command=os.environ.get("TRANQUIL_JUDGE_COMMAND") or None,
        notification_webhook_url=os.environ.get("TRANQUIL_NOTIFICATION_WEBHOOK_URL") or None,
        notification_command=os.environ.get("TRANQUIL_NOTIFICATION_COMMAND") or None,
        trace_sampling_enabled=env_bool("TRANQUIL_TRACE_SAMPLING_ENABLED", False),
        trace_sample_rate=float(os.environ.get("TRANQUIL_TRACE_SAMPLE_RATE", "0.05")),
        trace_sample_suite=os.environ.get("TRANQUIL_TRACE_SAMPLE_SUITE") or "sampled",
        sync_endpoint=os.environ.get("TRANQUIL_SYNC_ENDPOINT") or None,
    )


def load_config(home: Path | None = None, create: bool = True) -> TranquilConfig:
    cfg = default_config(home)
    path = cfg.config_path
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        cfg.host = str(data.get("host", cfg.host))
        cfg.port = int(data.get("port", cfg.port))
        cfg.token = str(os.environ.get("TRANQUIL_TOKEN") or data.get("token") or cfg.token)
        cfg.db_path = Path(data.get("db_path", cfg.db_path)).expanduser()
        cfg.retention_days = int(data.get("retention_days", cfg.retention_days))
        cfg.raw_payloads = bool(data.get("raw_payloads", cfg.raw_payloads))
        cfg.transcript_paths = [str(Path(p).expanduser()) for p in data.get("transcript_paths", cfg.transcript_paths)]
        cfg.codex_rollout_paths = [str(Path(p).expanduser()) for p in data.get("codex_rollout_paths", cfg.codex_rollout_paths)]
        cfg.tail_interval_seconds = float(data.get("tail_interval_seconds", cfg.tail_interval_seconds))
        cfg.kill_switch_enabled = bool(data.get("kill_switch_enabled", cfg.kill_switch_enabled))
        cfg.run_cost_budget_usd = float(data.get("run_cost_budget_usd", cfg.run_cost_budget_usd))
        cfg.replay_command = data.get("replay_command") or None
        cfg.editor_command = os.environ.get("TRANQUIL_EDITOR_COMMAND") or data.get("editor_command") or None
        cfg.judge_command = os.environ.get("TRANQUIL_JUDGE_COMMAND") or data.get("judge_command") or None
        cfg.notification_webhook_url = (
            os.environ.get("TRANQUIL_NOTIFICATION_WEBHOOK_URL") or data.get("notification_webhook_url") or None
        )
        cfg.notification_command = os.environ.get("TRANQUIL_NOTIFICATION_COMMAND") or data.get("notification_command") or None
        cfg.policy_enabled = bool(data.get("policy_enabled", cfg.policy_enabled))
        cfg.policy_forbidden_paths = [str(path) for path in data.get("policy_forbidden_paths", cfg.policy_forbidden_paths)]
        cfg.policy_forbidden_commands = [
            str(pattern) for pattern in data.get("policy_forbidden_commands", cfg.policy_forbidden_commands)
        ]
        cfg.trace_sampling_enabled = env_bool(
            "TRANQUIL_TRACE_SAMPLING_ENABLED",
            bool(data.get("trace_sampling_enabled", cfg.trace_sampling_enabled)),
        )
        cfg.trace_sample_rate = float(os.environ.get("TRANQUIL_TRACE_SAMPLE_RATE", data.get("trace_sample_rate", cfg.trace_sample_rate)))
        cfg.trace_sample_suite = str(os.environ.get("TRANQUIL_TRACE_SAMPLE_SUITE") or data.get("trace_sample_suite", cfg.trace_sample_suite))
        cfg.sync_endpoint = os.environ.get("TRANQUIL_SYNC_ENDPOINT") or data.get("sync_endpoint") or None
        raw_headers = data.get("sync_headers", cfg.sync_headers)
        cfg.sync_headers = {str(key): str(value) for key, value in raw_headers.items()} if isinstance(raw_headers, dict) else {}
        thresholds = data.get("signal_thresholds", {})
        cfg.signal_thresholds = SignalThresholds(
            loop_repeats=int(thresholds.get("loop_repeats", cfg.signal_thresholds.loop_repeats)),
            reread_repeats=int(thresholds.get("reread_repeats", cfg.signal_thresholds.reread_repeats)),
            failure_cascade_count=int(thresholds.get("failure_cascade_count", cfg.signal_thresholds.failure_cascade_count)),
            runaway_cost_usd=float(thresholds.get("runaway_cost_usd", cfg.signal_thresholds.runaway_cost_usd)),
            runaway_cost_per_min_usd=float(thresholds.get("runaway_cost_per_min_usd", cfg.signal_thresholds.runaway_cost_per_min_usd)),
            idle_minutes=int(thresholds.get("idle_minutes", cfg.signal_thresholds.idle_minutes)),
            scheduled_idle_minutes=int(thresholds.get("scheduled_idle_minutes", cfg.signal_thresholds.scheduled_idle_minutes)),
        )
    elif create:
        save_config(cfg)
    return cfg


def save_config(config: TranquilConfig) -> None:
    config.home.mkdir(parents=True, exist_ok=True)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "token": config.token,
        "db_path": str(config.db_path),
        "retention_days": config.retention_days,
        "raw_payloads": config.raw_payloads,
        "transcript_paths": config.transcript_paths,
        "codex_rollout_paths": config.codex_rollout_paths,
        "tail_interval_seconds": config.tail_interval_seconds,
        "kill_switch_enabled": config.kill_switch_enabled,
        "run_cost_budget_usd": config.run_cost_budget_usd,
        "signal_thresholds": {
            "loop_repeats": config.signal_thresholds.loop_repeats,
            "reread_repeats": config.signal_thresholds.reread_repeats,
            "failure_cascade_count": config.signal_thresholds.failure_cascade_count,
            "runaway_cost_usd": config.signal_thresholds.runaway_cost_usd,
            "runaway_cost_per_min_usd": config.signal_thresholds.runaway_cost_per_min_usd,
            "idle_minutes": config.signal_thresholds.idle_minutes,
            "scheduled_idle_minutes": config.signal_thresholds.scheduled_idle_minutes,
        },
    }
    if config.replay_command:
        payload["replay_command"] = config.replay_command
    if config.editor_command:
        payload["editor_command"] = config.editor_command
    if config.judge_command:
        payload["judge_command"] = config.judge_command
    if config.notification_webhook_url:
        payload["notification_webhook_url"] = config.notification_webhook_url
    if config.notification_command:
        payload["notification_command"] = config.notification_command
    if config.policy_enabled:
        payload["policy_enabled"] = config.policy_enabled
    if config.policy_forbidden_paths:
        payload["policy_forbidden_paths"] = config.policy_forbidden_paths
    if config.policy_forbidden_commands:
        payload["policy_forbidden_commands"] = config.policy_forbidden_commands
    if config.trace_sampling_enabled:
        payload["trace_sampling_enabled"] = config.trace_sampling_enabled
    if config.trace_sample_rate != 0.05:
        payload["trace_sample_rate"] = config.trace_sample_rate
    if config.trace_sample_suite != "sampled":
        payload["trace_sample_suite"] = config.trace_sample_suite
    if config.sync_endpoint:
        payload["sync_endpoint"] = config.sync_endpoint
    if config.sync_headers:
        payload["sync_headers"] = config.sync_headers
    config.config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def update_config(home: Path | None = None, **changes: Any) -> TranquilConfig:
    cfg = load_config(home=home, create=True)
    for key, value in changes.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    save_config(cfg)
    return cfg


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() not in {"0", "false", "no", "off", ""}
