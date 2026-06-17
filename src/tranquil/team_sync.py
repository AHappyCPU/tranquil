from __future__ import annotations

import json
import urllib.request
from typing import Any

from .storage import Storage
from .util import iso_now


def push_sync(storage: Storage, endpoint: str, headers: dict[str, str] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    data = storage.export_data()
    payload = {
        "schema": "tranquil.sync/v1",
        "exported_at": iso_now(),
        "data": data,
    }
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response_body = response.read().decode("utf-8")
        status = response.status
    return {
        "status": status,
        "events": len(data["events"]),
        "runs": len(data["runs"]),
        "fixtures": len(data["fixtures"]),
        "scores": len(data["scores"]),
        "response": response_body[:2000],
    }
