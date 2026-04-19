"""Tests for BotState.load corrupt-file fallback."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from osbot.state.bot_state import BotState
from osbot.types import ScoredIssue

if TYPE_CHECKING:
    from pathlib import Path


def _issue(repo: str, number: int, score: float) -> ScoredIssue:
    return ScoredIssue(repo=repo, number=number, title=f"Issue {number}", score=score)


async def test_load_missing_file_is_noop(tmp_path: Path) -> None:
    state = BotState(tmp_path / "state.json")
    await state.load()
    assert state.issue_queue == []
    assert state.active_work == {}
    assert state.open_prs == []


async def test_load_valid_file_hydrates(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    writer = BotState(path)
    await writer.enqueue([_issue("a/b", 1, 5.0)])

    reader = BotState(path)
    await reader.load()
    assert len(reader.issue_queue) == 1
    assert reader.issue_queue[0].repo == "a/b"


async def test_load_corrupt_json_falls_back_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{this is not valid json")

    state = BotState(path)
    await state.load()

    assert state.issue_queue == []
    assert state.active_work == {}
    assert state.open_prs == []
    # Original file should be quarantined so the next flush writes a fresh file.
    assert not path.exists()
    quarantined = list(tmp_path.glob("state.corrupt-*"))
    assert len(quarantined) == 1


async def test_load_wrong_shape_falls_back_to_empty(tmp_path: Path) -> None:
    """A JSON file with the wrong structure (e.g. list instead of dict, or
    dataclass fields that don't match) should not crash the container."""
    path = tmp_path / "state.json"
    # Valid JSON, valid dict shape at top level, but issue_queue entries
    # are missing required fields -> ScoredIssue(**i) will raise TypeError.
    path.write_text(json.dumps({"issue_queue": [{"bogus": "field"}]}))

    state = BotState(path)
    await state.load()

    assert state.issue_queue == []
    assert not path.exists()
    assert len(list(tmp_path.glob("state.corrupt-*"))) == 1


async def test_load_recovers_after_quarantine(tmp_path: Path) -> None:
    """After a corrupt load, the next flush must write a usable state file."""
    path = tmp_path / "state.json"
    path.write_text("not json")

    state = BotState(path)
    await state.load()
    await state.enqueue([_issue("a/b", 1, 5.0)])

    assert path.exists()
    reloaded = BotState(path)
    await reloaded.load()
    assert len(reloaded.issue_queue) == 1
