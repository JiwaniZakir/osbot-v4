"""Docker health check — verifies the bot process is *actually alive*.

Runs every 60s via the HEALTHCHECK directive in the Dockerfile. A failure marks
the container as unhealthy, which is visible to `docker ps`, `docker inspect`,
and any external monitoring hooked into `{{.State.Health.Status}}`.

Checks, in order of cheapness:

1. **State dir present + writable.** Smoke test for the volume mount.
2. **`state.json` parses as a dict** (if it exists). Guards against partial writes.
3. **Heartbeat fresh.** `state/heartbeat.json` must have been written within
   `2 * OSBOT_CYCLE_INTERVAL_SEC` (default 1200s = 20 min). The orchestrator
   writes this at startup and after every cycle; a stale heartbeat means the
   event loop is wedged, crashed, or mid-restart.

A separate Dockerfile `--start-period` gives the container enough runway to
write its first heartbeat before this check starts mattering.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

STATE_DIR = Path(os.environ.get("OSBOT_STATE_DIR", "/opt/osbot/state"))
DEFAULT_CYCLE_SEC = 600
HEARTBEAT_STALE_MULTIPLIER = 2  # Fail if heartbeat older than N cycles.


def _cycle_interval_sec() -> int:
    raw = os.environ.get("OSBOT_CYCLE_INTERVAL_SEC", "")
    try:
        val = int(raw) if raw else DEFAULT_CYCLE_SEC
    except ValueError:
        return DEFAULT_CYCLE_SEC
    return val if val > 0 else DEFAULT_CYCLE_SEC


def _check_state_dir() -> bool:
    if not STATE_DIR.exists():
        print("FAIL: state dir missing")
        return False
    try:
        probe = STATE_DIR / ".healthcheck"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        print(f"FAIL: state dir not writable: {exc}")
        return False
    return True


def _check_state_json() -> bool:
    path = STATE_DIR / "state.json"
    if not path.exists():
        return True  # Fresh install — bot hasn't written state yet.
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: state.json corrupt: {exc}")
        return False
    if not isinstance(data, dict):
        print("FAIL: state.json is not a dict")
        return False
    return True


def _check_heartbeat() -> bool:
    path = STATE_DIR / "heartbeat.json"
    if not path.exists():
        print("FAIL: heartbeat.json missing — bot never started or state volume lost")
        return False
    try:
        data = json.loads(path.read_text())
        ts_raw = data["timestamp"]
        ts = datetime.fromisoformat(ts_raw)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"FAIL: heartbeat.json unreadable: {exc}")
        return False

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - ts).total_seconds()
    max_age = _cycle_interval_sec() * HEARTBEAT_STALE_MULTIPLIER
    if age > max_age:
        print(f"FAIL: heartbeat stale ({age:.0f}s > {max_age}s) — event loop likely crashed or wedged")
        return False
    return True


def check() -> bool:
    return _check_state_dir() and _check_state_json() and _check_heartbeat()


if __name__ == "__main__":
    sys.exit(0 if check() else 1)
