from __future__ import annotations

import subprocess
import threading
import urllib.request
from typing import Any

from .config import TranquilConfig
from .util import json_dumps


class SignalNotifier:
    def __init__(self, config: TranquilConfig):
        self.config = config
        self.errors: list[str] = []
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.config.notification_webhook_url or self.config.notification_command)

    def notify_signal(self, signal: dict[str, Any]) -> None:
        if not self.enabled:
            return
        payload = {"type": "signal", "signal": signal}
        thread = threading.Thread(target=self._deliver, args=(payload,), name="tranquil-signal-notifier", daemon=True)
        thread.start()

    def deliver_sync(self, signal: dict[str, Any]) -> None:
        """Deliver a signal on the calling thread.

        Use this from short-lived processes (e.g. the command-hook ingester)
        where a daemon thread would be killed before the webhook/command runs.
        Each transport already bounds itself with a timeout and records errors.
        """
        if not self.enabled:
            return
        self._deliver({"type": "signal", "signal": signal})

    def _deliver(self, payload: dict[str, Any]) -> None:
        body = json_dumps(payload)
        if self.config.notification_webhook_url:
            self._post_webhook(self.config.notification_webhook_url, body)
        if self.config.notification_command:
            self._run_command(self.config.notification_command, body)

    def _post_webhook(self, url: str, body: str) -> None:
        request = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3.0) as response:
                response.read()
        except Exception as exc:  # pragma: no cover - exposed through health in server use
            self._record_error(f"webhook {type(exc).__name__}: {exc}")

    def _run_command(self, command: str, body: str) -> None:
        try:
            subprocess.run(
                command,
                shell=True,
                input=body,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except Exception as exc:  # pragma: no cover - exposed through health in server use
            self._record_error(f"command {type(exc).__name__}: {exc}")

    def _record_error(self, message: str) -> None:
        with self._lock:
            self.errors.append(message)
            del self.errors[:-20]
