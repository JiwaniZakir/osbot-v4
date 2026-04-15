"""Tests for `deploy/health_check.py` — liveness probe logic."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parent.parent / "deploy" / "health_check.py"


@pytest.fixture
def healthcheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Load health_check.py with STATE_DIR pointed at a tmp dir."""
    monkeypatch.setenv("OSBOT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OSBOT_CYCLE_INTERVAL_SEC", "600")
    # deploy/health_check.py isn't in a package — load by path.
    spec = importlib.util.spec_from_file_location("health_check_under_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["health_check_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _write_heartbeat(state_dir: Path, age_seconds: float, cycle: int = 1) -> None:
    ts = datetime.now(UTC) - timedelta(seconds=age_seconds)
    (state_dir / "heartbeat.json").write_text(json.dumps({"timestamp": ts.isoformat(), "cycle": cycle, "pid": 1}))


def test_fresh_heartbeat_passes(healthcheck, tmp_path: Path) -> None:
    _write_heartbeat(tmp_path, age_seconds=5)
    assert healthcheck.check() is True


def test_missing_heartbeat_fails(healthcheck) -> None:
    assert healthcheck.check() is False


def test_stale_heartbeat_fails(healthcheck, tmp_path: Path) -> None:
    # 1500s > 2 * 600s cycle interval
    _write_heartbeat(tmp_path, age_seconds=1500)
    assert healthcheck.check() is False


def test_heartbeat_at_exactly_threshold_passes(healthcheck, tmp_path: Path) -> None:
    # Just inside the 2x cycle window.
    _write_heartbeat(tmp_path, age_seconds=1100)
    assert healthcheck.check() is True


def test_corrupt_heartbeat_fails(healthcheck, tmp_path: Path) -> None:
    (tmp_path / "heartbeat.json").write_text("{not-json")
    assert healthcheck.check() is False


def test_missing_timestamp_field_fails(healthcheck, tmp_path: Path) -> None:
    (tmp_path / "heartbeat.json").write_text(json.dumps({"cycle": 1}))
    assert healthcheck.check() is False


def test_corrupt_state_json_fails(healthcheck, tmp_path: Path) -> None:
    _write_heartbeat(tmp_path, age_seconds=5)
    (tmp_path / "state.json").write_text("{not-json")
    assert healthcheck.check() is False


def test_state_json_missing_is_fine_if_heartbeat_fresh(healthcheck, tmp_path: Path) -> None:
    _write_heartbeat(tmp_path, age_seconds=5)
    # state.json absent — accept for fresh installs.
    assert healthcheck.check() is True


def test_state_json_wrong_type_fails(healthcheck, tmp_path: Path) -> None:
    _write_heartbeat(tmp_path, age_seconds=5)
    (tmp_path / "state.json").write_text('["not","a","dict"]')
    assert healthcheck.check() is False


def test_state_dir_missing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "nonexistent"
    monkeypatch.setenv("OSBOT_STATE_DIR", str(missing))
    monkeypatch.setenv("OSBOT_CYCLE_INTERVAL_SEC", "600")
    spec = importlib.util.spec_from_file_location("health_check_missing_dir", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.check() is False


def test_invalid_cycle_interval_env_falls_back_to_default(
    healthcheck, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSBOT_CYCLE_INTERVAL_SEC", "not-a-number")
    # Reload so the module re-reads env.
    spec = importlib.util.spec_from_file_location("health_check_bad_env", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _write_heartbeat(tmp_path, age_seconds=5)
    # STATE_DIR was resolved at import — re-resolve via module attribute.
    module.STATE_DIR = tmp_path
    assert module.check() is True
