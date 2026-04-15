"""Liveness heartbeat — proves the orchestrator event loop is actually running.

Written by `orchestrator.loop` at the end of every cycle + once immediately after
startup health check passes. Read by `deploy/health_check.py` to distinguish a
live bot from a container whose filesystem is healthy but whose process is
wedged, crashed, or stuck in a restart loop.

File layout (`state/heartbeat.json`):

    {
      "timestamp": "2026-04-15T16:34:59.455938+00:00",  # ISO 8601, UTC
      "cycle": 42,                                       # monotonically increasing
      "pid": 1                                           # container PID of the writer
    }

Writes are atomic (tempfile + os.rename) so a crash mid-write never leaves a
partial file for the healthcheck to misread.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

HEARTBEAT_FILENAME = "heartbeat.json"


def write_heartbeat(state_dir: Path, cycle: int) -> None:
    """Write a fresh heartbeat record. Atomic — safe against mid-write crash."""
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "cycle": cycle,
        "pid": os.getpid(),
    }
    target = state_dir / HEARTBEAT_FILENAME
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, target)


def read_heartbeat(state_dir: Path) -> dict[str, object] | None:
    """Return the heartbeat payload, or None if missing / unreadable."""
    path = state_dir / HEARTBEAT_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data
