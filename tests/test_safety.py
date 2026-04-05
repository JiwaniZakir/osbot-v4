"""Tests for domain filter, circuit breaker, and anti-spam.

Covers domain enforcement (language + topics), repo bans (active/expired),
and blacklisted orgs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from osbot.safety.anti_spam import check_spam
from osbot.safety.circuit_breaker import ban_repo, can_attempt_repo
from osbot.safety.domain import is_in_domain
from osbot.types import RepoMeta

if TYPE_CHECKING:
    from osbot.state.db import MemoryDB

# ---------------------------------------------------------------------------
# Domain filter
# ---------------------------------------------------------------------------


async def test_python_ai_repo_passes_domain() -> None:
    """Python repo with AI topic should pass domain check."""
    repo = RepoMeta(
        owner="org",
        name="ai-lib",
        language="Python",
        stars=1000,
        topics=["ai", "python"],
    )
    assert is_in_domain(repo) is True


async def test_php_repo_fails_domain() -> None:
    """PHP repo should fail the language check."""
    repo = RepoMeta(
        owner="org",
        name="web-app",
        language="PHP",
        stars=1000,
        topics=["ai", "web"],
    )
    assert is_in_domain(repo) is False


async def test_no_topic_match_fails_domain() -> None:
    """Python repo without any domain keyword topic should fail."""
    repo = RepoMeta(
        owner="org",
        name="utils",
        language="Python",
        stars=1000,
        topics=["cli", "utilities", "devtools"],
    )
    assert is_in_domain(repo) is False


async def test_typescript_with_llm_passes_domain() -> None:
    """TypeScript repo with LLM topic should pass."""
    repo = RepoMeta(
        owner="org",
        name="ts-llm",
        language="TypeScript",
        stars=500,
        topics=["llm", "typescript"],
    )
    assert is_in_domain(repo) is True


async def test_no_language_fails_domain() -> None:
    """Repo with no language set should fail domain check."""
    repo = RepoMeta(
        owner="org",
        name="unknown",
        language="",
        stars=1000,
        topics=["ai"],
    )
    assert is_in_domain(repo) is False


# ---------------------------------------------------------------------------
# Circuit breaker: repo bans
# ---------------------------------------------------------------------------


async def test_ban_repo_and_check(db: MemoryDB) -> None:
    """Banning a repo should make can_attempt_repo return False."""
    await ban_repo("owner/repo", 7, "test ban", db, "test")
    ok, reason = await can_attempt_repo("owner/repo", db)
    assert not ok
    assert "banned" in reason


async def test_expired_ban_allows(db: MemoryDB) -> None:
    """A ban that has expired should allow the repo."""
    # Insert a ban that expired yesterday
    yesterday = datetime.now(UTC) - timedelta(days=1)
    await db.execute(
        "INSERT INTO repo_bans (repo, reason, banned_at, expires_at, created_by) VALUES (?, ?, ?, ?, ?)",
        (
            "owner/old",
            "old ban",
            (yesterday - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            yesterday.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "test",
        ),
    )
    ok, reason = await can_attempt_repo("owner/old", db)
    assert ok
    assert reason == ""


# ---------------------------------------------------------------------------
# Anti-spam: blacklisted orgs
# ---------------------------------------------------------------------------


async def test_blacklisted_org_blocked(db: MemoryDB) -> None:
    """scikit-learn org is blacklisted and should be blocked."""
    ok, reason = await check_spam("scikit-learn/scikit-learn", db)
    assert not ok
    assert "blacklisted" in reason


async def test_non_blacklisted_org_passes(db: MemoryDB) -> None:
    """A non-blacklisted org should pass the spam check."""
    ok, reason = await check_spam("pytorch/pytorch", db)
    assert ok
    assert reason == ""
