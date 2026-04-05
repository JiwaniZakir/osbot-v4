"""Tests for MemoryDB and BotState.

Covers repo facts, outcomes, bans, and BotState queue operations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from osbot.state.bot_state import BotState
from osbot.state.db import MemoryDB
from osbot.types import Outcome, ScoredIssue


# ---------------------------------------------------------------------------
# MemoryDB: repo facts
# ---------------------------------------------------------------------------


async def test_set_and_get_repo_fact(db: MemoryDB) -> None:
    """set_repo_fact should store and get_repo_fact should retrieve."""
    await db.set_repo_fact("owner/repo", "test_cmd", "pytest", "contributing_md", 0.8)
    val = await db.get_repo_fact("owner/repo", "test_cmd")
    assert val == "pytest"


async def test_fact_conflict_resolution(db: MemoryDB) -> None:
    """Setting a fact twice should archive the old value and return the new one."""
    await db.set_repo_fact("owner/repo", "test_cmd", "pytest", "contributing_md", 0.8)
    await db.set_repo_fact("owner/repo", "test_cmd", "make test", "outcome_analysis", 0.9)

    val = await db.get_repo_fact("owner/repo", "test_cmd")
    assert val == "make test"

    # Verify the old fact was archived (valid_until set), not deleted
    all_facts = await db.fetchall(
        "SELECT * FROM repo_facts WHERE repo = ? AND key = ?",
        ("owner/repo", "test_cmd"),
    )
    assert len(all_facts) == 2
    archived = [f for f in all_facts if f["valid_until"] is not None]
    assert len(archived) == 1
    assert archived[0]["value"] == "pytest"


# ---------------------------------------------------------------------------
# MemoryDB: outcomes
# ---------------------------------------------------------------------------


async def test_record_and_get_outcome(db: MemoryDB) -> None:
    """Recording an outcome should be retrievable."""
    await db.record_outcome(
        "owner/repo", 42, 100, Outcome.SUBMITTED, None, 5000
    )
    row = await db.get_outcome("owner/repo", 42)
    assert row is not None
    assert row["outcome"] == "submitted"
    assert row["pr_number"] == 100
    assert row["tokens_used"] == 5000


async def test_get_outcome_returns_latest(db: MemoryDB) -> None:
    """Multiple outcomes for the same issue should return the latest."""
    await db.record_outcome("owner/repo", 42, 100, Outcome.SUBMITTED)
    await db.record_outcome("owner/repo", 42, 100, Outcome.MERGED)
    row = await db.get_outcome("owner/repo", 42)
    assert row is not None
    assert row["outcome"] == "merged"


async def test_get_outcome_none_for_missing(db: MemoryDB) -> None:
    """get_outcome should return None when no outcome exists."""
    row = await db.get_outcome("owner/repo", 999)
    assert row is None


# ---------------------------------------------------------------------------
# MemoryDB: bans
# ---------------------------------------------------------------------------


async def test_ban_and_check(db: MemoryDB) -> None:
    """Banning a repo should be detected by is_repo_banned."""
    assert not await db.is_repo_banned("owner/repo")
    await db.ban_repo("owner/repo", "test ban", 7, "test")
    assert await db.is_repo_banned("owner/repo")


async def test_expired_ban(db: MemoryDB) -> None:
    """An expired ban should not be reported as active."""
    # Insert an already-expired ban
    past = datetime.now(timezone.utc) - timedelta(days=1)
    past_start = past - timedelta(days=7)
    await db.execute(
        """
        INSERT INTO repo_bans (repo, reason, banned_at, expires_at, created_by)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "owner/expired",
            "old ban",
            past_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            past.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "test",
        ),
    )
    assert not await db.is_repo_banned("owner/expired")


# ---------------------------------------------------------------------------
# MemoryDB: transactions
# ---------------------------------------------------------------------------


async def test_transaction_commits(db: MemoryDB) -> None:
    """Transaction should commit on success."""
    async with db.transaction():
        await db.execute(
            "INSERT INTO repo_facts (repo, key, value, source, confidence, valid_from, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a/b", "k", "v", "test", 0.5, "2026-01-01", "2026-01-01"),
        )
    val = await db.get_repo_fact("a/b", "k")
    assert val == "v"


async def test_transaction_rolls_back_on_error(db: MemoryDB) -> None:
    """Transaction should rollback on exception."""
    try:
        async with db.transaction():
            await db.execute(
                "INSERT INTO repo_facts (repo, key, value, source, confidence, valid_from, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("a/b", "k2", "v2", "test", 0.5, "2026-01-01", "2026-01-01"),
            )
            raise ValueError("boom")
    except ValueError:
        pass
    val = await db.get_repo_fact("a/b", "k2")
    assert val is None


# ---------------------------------------------------------------------------
# BotState: queue operations
# ---------------------------------------------------------------------------


def _issue(repo: str, number: int, score: float) -> ScoredIssue:
    return ScoredIssue(repo=repo, number=number, title=f"Issue {number}", score=score)


async def test_bot_state_pop_issue(tmp_path: Path) -> None:
    """pop_issue should return the highest-scored issue matching predicate."""
    state = BotState(tmp_path / "state.json")
    await state.enqueue([_issue("a/b", 1, 5.0), _issue("a/b", 2, 8.0), _issue("c/d", 3, 9.0)])

    # Pop only from a/b
    issue = await state.pop_issue(lambda i: i.repo == "a/b")
    assert issue is not None
    assert issue.number == 2  # highest score in a/b
    assert issue.score == 8.0

    # a/b#1 should still be in queue
    issue2 = await state.pop_issue(lambda i: i.repo == "a/b")
    assert issue2 is not None
    assert issue2.number == 1


async def test_bot_state_pop_empty(tmp_path: Path) -> None:
    """pop_issue on empty queue should return None."""
    state = BotState(tmp_path / "state.json")
    result = await state.pop_issue()
    assert result is None


async def test_bot_state_dedup(tmp_path: Path) -> None:
    """Enqueueing a duplicate issue with higher score should replace the old one."""
    state = BotState(tmp_path / "state.json")
    await state.enqueue([_issue("a/b", 1, 5.0)])
    await state.enqueue([_issue("a/b", 1, 8.0)])

    # Should have only one entry
    assert len(state.issue_queue) == 1
    assert state.issue_queue[0].score == 8.0


async def test_bot_state_dedup_lower_score_ignored(tmp_path: Path) -> None:
    """Enqueueing a duplicate with a lower score should keep the original."""
    state = BotState(tmp_path / "state.json")
    await state.enqueue([_issue("a/b", 1, 8.0)])
    await state.enqueue([_issue("a/b", 1, 3.0)])

    assert len(state.issue_queue) == 1
    assert state.issue_queue[0].score == 8.0


async def test_bot_state_persistence(tmp_path: Path) -> None:
    """State should persist and survive reload."""
    path = tmp_path / "state.json"
    state1 = BotState(path)
    await state1.enqueue([_issue("a/b", 1, 7.0)])

    # Load into a new BotState
    state2 = BotState(path)
    await state2.load()
    assert len(state2.issue_queue) == 1
    assert state2.issue_queue[0].repo == "a/b"
    assert state2.issue_queue[0].score == 7.0
