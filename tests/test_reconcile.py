"""Tests for `osbot.orchestrator.reconcile` — orphan PR adoption on startup."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from osbot.orchestrator.reconcile import reconcile_open_prs
from osbot.state import BotState
from osbot.types import OpenPR

if TYPE_CHECKING:
    from pathlib import Path


def _fake_gh_prs(entries: list[dict]) -> bytes:
    return json.dumps(entries).encode()


def _mock_proc(stdout: bytes, returncode: int = 0):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


@pytest.fixture
def fresh_state(tmp_path: Path) -> BotState:
    return BotState(tmp_path / "state.json")


async def test_no_username_short_circuits(fresh_state: BotState) -> None:
    with patch("osbot.orchestrator.reconcile.settings") as mock_settings:
        mock_settings.github_username = ""
        adopted = await reconcile_open_prs(fresh_state)
    assert adopted == 0


async def test_adopts_orphans_not_in_state(fresh_state: BotState) -> None:
    gh_output = _fake_gh_prs(
        [
            {
                "number": 101,
                "repository": {"nameWithOwner": "foo/bar"},
                "headRefName": "fix/42",
                "createdAt": "2026-04-01T12:00:00Z",
            },
            {
                "number": 202,
                "repository": {"nameWithOwner": "baz/qux"},
                "headRefName": "fix/99",
                "createdAt": "2026-04-02T08:30:00Z",
            },
        ]
    )
    with (
        patch("osbot.orchestrator.reconcile.settings") as mock_settings,
        patch("asyncio.create_subprocess_exec", return_value=_mock_proc(gh_output)),
    ):
        mock_settings.github_username = "botaccount"
        adopted = await reconcile_open_prs(fresh_state)

    assert adopted == 2
    prs = await fresh_state.get_open_prs()
    assert {(p.repo, p.pr_number) for p in prs} == {("foo/bar", 101), ("baz/qux", 202)}


async def test_skips_already_tracked_prs(fresh_state: BotState) -> None:
    await fresh_state.add_open_pr(
        OpenPR(
            repo="foo/bar",
            issue_number=42,
            pr_number=101,
            url="https://github.com/foo/bar/pull/101",
            branch="fix/42",
            submitted_at="2026-04-01T12:00:00Z",
        )
    )
    gh_output = _fake_gh_prs(
        [
            {
                "number": 101,
                "repository": {"nameWithOwner": "foo/bar"},
                "headRefName": "fix/42",
                "createdAt": "2026-04-01T12:00:00Z",
            }
        ]
    )
    with (
        patch("osbot.orchestrator.reconcile.settings") as mock_settings,
        patch("asyncio.create_subprocess_exec", return_value=_mock_proc(gh_output)),
    ):
        mock_settings.github_username = "botaccount"
        adopted = await reconcile_open_prs(fresh_state)

    assert adopted == 0
    assert len(await fresh_state.get_open_prs()) == 1


async def test_skips_self_owned_repos(fresh_state: BotState) -> None:
    gh_output = _fake_gh_prs(
        [
            {
                "number": 53,
                "repository": {"nameWithOwner": "botaccount/osbot-v4"},
                "headRefName": "feat/x",
                "createdAt": "2026-04-15T12:00:00Z",
            },
            {
                "number": 101,
                "repository": {"nameWithOwner": "foo/bar"},
                "headRefName": "fix/42",
                "createdAt": "2026-04-15T12:00:00Z",
            },
        ]
    )
    with (
        patch("osbot.orchestrator.reconcile.settings") as mock_settings,
        patch("asyncio.create_subprocess_exec", return_value=_mock_proc(gh_output)),
    ):
        mock_settings.github_username = "botaccount"
        adopted = await reconcile_open_prs(fresh_state)

    assert adopted == 1
    prs = await fresh_state.get_open_prs()
    assert len(prs) == 1
    assert prs[0].repo == "foo/bar"


async def test_gh_failure_returns_zero(fresh_state: BotState) -> None:
    with (
        patch("osbot.orchestrator.reconcile.settings") as mock_settings,
        patch("asyncio.create_subprocess_exec", return_value=_mock_proc(b"", returncode=1)),
    ):
        mock_settings.github_username = "botaccount"
        adopted = await reconcile_open_prs(fresh_state)
    assert adopted == 0
    assert await fresh_state.get_open_prs() == []


async def test_malformed_json_returns_zero(fresh_state: BotState) -> None:
    with (
        patch("osbot.orchestrator.reconcile.settings") as mock_settings,
        patch("asyncio.create_subprocess_exec", return_value=_mock_proc(b"not-json")),
    ):
        mock_settings.github_username = "botaccount"
        adopted = await reconcile_open_prs(fresh_state)
    assert adopted == 0


async def test_partial_entries_skipped(fresh_state: BotState) -> None:
    gh_output = _fake_gh_prs(
        [
            {"number": 101, "repository": {"nameWithOwner": "foo/bar"}},  # valid, missing optional
            {"number": None, "repository": {"nameWithOwner": "x/y"}},  # invalid
            {"number": 202, "repository": {}},  # invalid
        ]
    )
    with (
        patch("osbot.orchestrator.reconcile.settings") as mock_settings,
        patch("asyncio.create_subprocess_exec", return_value=_mock_proc(gh_output)),
    ):
        mock_settings.github_username = "botaccount"
        adopted = await reconcile_open_prs(fresh_state)
    assert adopted == 1
    prs = await fresh_state.get_open_prs()
    assert prs[0].pr_number == 101
