"""Circuit breakers -- automatic repo banning on repeated failures.

Persisted in ``memory.db`` ``repo_bans`` table so bans survive restarts.
Checked in preflight BEFORE any Claude call.

Signal                                -> Action
--------------------------------------------
Same repo, same error, 3+ times      -> Ban 7 days + clear queue
Planning timeout 2x on same repo     -> Score -2.0, ban at 4x
5 consecutive failures (any repo)    -> Ban 7 days
Language/domain filter fails          -> Permanent removal
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import MemoryDBProtocol

logger = get_logger(__name__)

# Thresholds
CONSECUTIVE_FAILURE_THRESHOLD = 5
TIMEOUT_WARN_THRESHOLD = 2
TIMEOUT_BAN_THRESHOLD = 4
SAME_ERROR_THRESHOLD = 3

# Default ban durations (days)
DEFAULT_BAN_DAYS = 7
TIMEOUT_BAN_DAYS = 14
REPEAT_ERROR_BAN_DAYS = 7


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _expires_iso(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


async def can_attempt_repo(
    repo: str,
    db: MemoryDBProtocol,
) -> tuple[bool, str]:
    """Check if *repo* is currently banned.

    Returns ``(True, "")`` if the repo is clear, or
    ``(False, reason)`` if a ban is active.
    """
    row = await db.fetchone(
        """
        SELECT reason, expires_at
        FROM repo_bans
        WHERE repo = ?
          AND expires_at > datetime('now')
        ORDER BY expires_at DESC
        LIMIT 1
        """,
        (repo,),
    )
    if row is not None:
        reason = row.get("reason", "banned")
        expires = row.get("expires_at", "unknown")
        logger.info("repo_banned", repo=repo, reason=reason, expires_at=expires)
        return False, f"banned: {reason} (until {expires})"

    return True, ""


async def ban_repo(
    repo: str,
    days: int,
    reason: str,
    db: MemoryDBProtocol,
    created_by: str,
) -> None:
    """Insert a ban into the ``repo_bans`` table (no-op if active ban exists)."""
    # Prevent duplicate bans piling up for the same repo
    existing = await db.fetchone(
        "SELECT id FROM repo_bans WHERE repo = ? AND expires_at > datetime('now') LIMIT 1",
        (repo,),
    )
    if existing:
        logger.debug("ban_already_active", repo=repo, reason=reason)
        return

    now = _now_iso()
    expires = _expires_iso(days)

    await db.execute(
        """
        INSERT INTO repo_bans (repo, reason, banned_at, expires_at, created_by)
        VALUES (?, ?, ?, ?, ?)
        """,
        (repo, reason, now, expires, created_by),
    )

    logger.warning(
        "repo_ban_created",
        repo=repo,
        days=days,
        reason=reason,
        created_by=created_by,
        expires_at=expires,
    )


def _error_category(failure_reason: str) -> str:
    """Map a failure_reason string to a canonical error category.

    Used to detect loops where the same underlying problem manifests with
    slightly different error messages (e.g., "scope creep" vs "too many files").
    """
    lower = (failure_reason or "").lower()
    if any(kw in lower for kw in ("scope", "too many file", "unrelated", "too large", "diff too")):
        return "scope_creep"
    if any(kw in lower for kw in ("timeout", "timed out", "time limit")):
        return "timeout"
    if any(kw in lower for kw in ("test fail", "tests fail", "test suite", "exit code 1", "pytest")):
        return "test_failure"
    if any(kw in lower for kw in ("lint", "ruff", "flake8", "mypy", "type error")):
        return "lint_failure"
    if any(kw in lower for kw in ("empty diff", "no changes", "nothing committed")):
        return "empty_diff"
    if any(kw in lower for kw in ("duplicate", "competing pr", "already open")):
        return "duplicate_pr"
    if any(kw in lower for kw in ("non-english", "non_english", "language")):
        return "non_english"
    return "other"


async def record_failure(
    repo: str,
    outcome: str,
    db: MemoryDBProtocol,
) -> None:
    """Record a failure and auto-ban if consecutive failures exceed threshold.

    Checks for two patterns:
    1. Same repo, same SEMANTIC error category, 3+ times -> ban 7 days.
       (catches "scope creep" vs "too many files" as the same root cause)
    2. Same repo, any error, 5+ consecutive times -> ban 7 days.
    """
    # Pattern 1: same semantic error category repeated
    category = _error_category(outcome)
    same_error_rows = await db.fetchall(
        """
        SELECT COUNT(*) AS cnt
        FROM outcomes
        WHERE repo = ?
          AND failure_reason = ?
          AND created_at > datetime('now', '-7 days')
        """,
        (repo, outcome),
    )
    same_error_count = same_error_rows[0].get("cnt", 0) if same_error_rows else 0

    # Also check semantic category across slightly-varied error messages
    if same_error_count < SAME_ERROR_THRESHOLD and category != "other":
        category_rows = await db.fetchall(
            """
            SELECT failure_reason
            FROM outcomes
            WHERE repo = ?
              AND outcome = 'rejected'
              AND created_at > datetime('now', '-7 days')
            LIMIT 20
            """,
            (repo,),
        )
        same_error_count = max(
            same_error_count,
            sum(1 for row in category_rows if _error_category(row.get("failure_reason", "")) == category),
        )

    if same_error_count >= SAME_ERROR_THRESHOLD:
        logger.warning(
            "circuit_breaker_trip",
            repo=repo,
            pattern="same_error_repeat",
            error=outcome,
            category=category,
            count=same_error_count,
        )
        await ban_repo(
            repo,
            REPEAT_ERROR_BAN_DAYS,
            f"same error category '{category}' {same_error_count}x in 7d: {outcome}",
            db,
            "circuit_breaker",
        )
        return

    # Pattern 2: consecutive failures regardless of error type
    recent_rows = await db.fetchall(
        """
        SELECT outcome
        FROM outcomes
        WHERE repo = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (repo, CONSECUTIVE_FAILURE_THRESHOLD),
    )

    if len(recent_rows) >= CONSECUTIVE_FAILURE_THRESHOLD:
        all_failed = all(row.get("outcome") not in ("merged", "iterated_merged") for row in recent_rows)
        if all_failed:
            logger.warning(
                "circuit_breaker_trip",
                repo=repo,
                pattern="consecutive_failures",
                count=CONSECUTIVE_FAILURE_THRESHOLD,
            )
            await ban_repo(
                repo,
                DEFAULT_BAN_DAYS,
                f"{CONSECUTIVE_FAILURE_THRESHOLD} consecutive failures",
                db,
                "circuit_breaker",
            )


async def record_timeout(
    repo: str,
    db: MemoryDBProtocol,
) -> None:
    """Track planning timeouts and escalate to ban at threshold.

    2x timeouts -> logged warning (score penalty handled by scorer).
    4x timeouts -> 14-day ban.
    """
    timeout_rows = await db.fetchall(
        """
        SELECT COUNT(*) AS cnt
        FROM outcomes
        WHERE repo = ?
          AND outcome = 'timeout'
          AND created_at > datetime('now', '-7 days')
        """,
        (repo,),
    )
    timeout_count = timeout_rows[0].get("cnt", 0) if timeout_rows else 0

    if timeout_count >= TIMEOUT_BAN_THRESHOLD:
        logger.warning(
            "circuit_breaker_trip",
            repo=repo,
            pattern="timeout_escalation",
            count=timeout_count,
        )
        await ban_repo(
            repo,
            TIMEOUT_BAN_DAYS,
            f"planning timeout {timeout_count}x in 7d",
            db,
            "circuit_breaker",
        )
    elif timeout_count >= TIMEOUT_WARN_THRESHOLD:
        logger.info(
            "timeout_warning",
            repo=repo,
            count=timeout_count,
            message="score penalty applied, ban at 4x",
        )
