from __future__ import annotations

import threading

from .config import TranquilConfig
from .notifications import SignalNotifier
from .storage import Storage
from .tailer import RolloutTailer, TranscriptTailer
from .tui import run_tui


def run_terminal_app(config: TranquilConfig, interval: float = 2.0) -> int:
    """Run the terminal Fleet view backed directly by SQLite.

    There is no HTTP collector. Live capture happens through command hooks that
    write to the same database; while this app is open it also runs the
    transcript and Codex rollout tailers as the durable backfill path.
    """
    notifier = SignalNotifier(config)
    storage = Storage(
        config.db_path,
        thresholds=config.signal_thresholds,
        raw_payloads=config.raw_payloads,
        signal_sink=notifier.notify_signal,
    )
    tailers: list[TranscriptTailer | RolloutTailer] = []
    if config.transcript_paths:
        tailers.append(TranscriptTailer(storage, config.transcript_paths, config.tail_interval_seconds))
    if config.codex_rollout_paths:
        tailers.append(RolloutTailer(storage, config.codex_rollout_paths, config.tail_interval_seconds))
    for tailer in tailers:
        tailer.start()
    try:
        return run_tui(storage, config.signal_thresholds, interval=interval)
    finally:
        for tailer in tailers:
            tailer.stop()
        for tailer in tailers:
            if isinstance(tailer, threading.Thread):
                tailer.join(timeout=2)
        storage.close()
