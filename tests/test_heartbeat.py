"""Tests for `osbot.state.heartbeat` — atomic write + read round-trips."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from osbot.state.heartbeat import HEARTBEAT_FILENAME, read_heartbeat, write_heartbeat

if TYPE_CHECKING:
    from pathlib import Path


def test_write_heartbeat_creates_file(tmp_path: Path) -> None:
    write_heartbeat(tmp_path, cycle=1)
    assert (tmp_path / HEARTBEAT_FILENAME).exists()


def test_write_heartbeat_payload_shape(tmp_path: Path) -> None:
    write_heartbeat(tmp_path, cycle=42)
    data = json.loads((tmp_path / HEARTBEAT_FILENAME).read_text())
    assert data["cycle"] == 42
    assert data["pid"] == os.getpid()
    # ISO 8601 timestamp, parseable, within the last minute.
    ts = datetime.fromisoformat(data["timestamp"])
    assert ts.tzinfo is not None
    assert (datetime.now(UTC) - ts).total_seconds() < 60


def test_write_heartbeat_overwrites_previous(tmp_path: Path) -> None:
    write_heartbeat(tmp_path, cycle=1)
    write_heartbeat(tmp_path, cycle=2)
    data = json.loads((tmp_path / HEARTBEAT_FILENAME).read_text())
    assert data["cycle"] == 2


def test_write_heartbeat_creates_state_dir(tmp_path: Path) -> None:
    nested = tmp_path / "new" / "nested"
    write_heartbeat(nested, cycle=0)
    assert (nested / HEARTBEAT_FILENAME).exists()


def test_write_heartbeat_leaves_no_tmp_file(tmp_path: Path) -> None:
    write_heartbeat(tmp_path, cycle=1)
    tmps = list(tmp_path.glob("*.tmp"))
    assert tmps == []


def test_read_heartbeat_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_heartbeat(tmp_path) is None


def test_read_heartbeat_returns_payload(tmp_path: Path) -> None:
    write_heartbeat(tmp_path, cycle=7)
    data = read_heartbeat(tmp_path)
    assert data is not None
    assert data["cycle"] == 7


def test_read_heartbeat_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / HEARTBEAT_FILENAME).write_text("{not-json")
    assert read_heartbeat(tmp_path) is None


def test_read_heartbeat_returns_none_on_non_dict(tmp_path: Path) -> None:
    (tmp_path / HEARTBEAT_FILENAME).write_text("[1, 2, 3]")
    assert read_heartbeat(tmp_path) is None
