"""Startup health check -- runs before the main loop, blocks until resolved.

Checks:
  - claude --version (no TOS dialog blocking)
  - gh auth status
  - State directory writable
  - memory.db healthy (tables exist)
  - Clears zombie active_work from previous run
"""

from __future__ import annotations

import os
from pathlib import Path

from osbot.config import settings
from osbot.gateway.github import GitHubCLI
from osbot.log import get_logger
from osbot.types import MemoryDBProtocol

logger = get_logger(__name__)


async def _check_claude() -> bool:
    """Verify ``claude --version`` responds (no TOS dialog blocking)."""
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        output = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            logger.error("health_claude_failed", returncode=proc.returncode, stderr=err[:300])
            return False

        # Detect TOS dialog: claude --version should return version text,
        # not an interactive prompt.
        if "terms" in err.lower() or "accept" in err.lower():
            logger.error("health_claude_tos", message="TOS dialog detected -- resolve manually")
            return False

        logger.info("health_claude_ok", version=output.strip()[:80])
        return True

    except asyncio.TimeoutError:
        logger.error("health_claude_timeout", message="claude --version timed out (possible TOS dialog)")
        return False
    except FileNotFoundError:
        logger.error("health_claude_missing", message="claude binary not found")
        return False


async def _check_github(github: GitHubCLI) -> bool:
    """Verify ``gh auth status`` succeeds."""
    result = await github.auth_status()
    if result.success:
        logger.info("health_github_ok")
        return True

    logger.error("health_github_failed", stderr=result.stderr[:300])
    return False


def _check_state_dir() -> bool:
    """Verify state directory exists and is writable."""
    state_dir: Path = settings.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    test_file = state_dir / ".health_check"
    try:
        test_file.write_text("ok")
        test_file.unlink()
        logger.info("health_state_dir_ok", path=str(state_dir))
        return True
    except OSError as exc:
        logger.error("health_state_dir_failed", path=str(state_dir), error=str(exc))
        return False


async def _check_memory_db(db: MemoryDBProtocol) -> bool:
    """Verify memory.db is accessible and has the expected tables."""
    required_tables = {"repo_facts", "outcomes", "repo_bans", "repo_signals"}

    try:
        rows = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'",
            (),
        )
        existing = {row.get("name", "") for row in rows}
        missing = required_tables - existing

        if missing:
            logger.error("health_db_missing_tables", missing=sorted(missing))
            return False

        logger.info("health_db_ok", tables=sorted(existing & required_tables))
        return True

    except Exception as exc:
        logger.error("health_db_failed", error=str(exc))
        return False


async def _clear_zombies(db: MemoryDBProtocol) -> None:
    """Clear active_work entries from a previous run that never completed.

    These are contributions or iterations that were in-flight when the
    process was killed.  We mark them as ``stuck`` in outcomes.
    """
    # If the state layer tracks active work in the DB, clean it up.
    # For state.json-based tracking, the orchestrator loop handles this
    # at load time.  This is a safety net for DB-tracked work.
    try:
        await db.execute(
            """
            UPDATE outcomes
            SET outcome = 'stuck', failure_reason = 'zombie_cleanup'
            WHERE outcome = 'in_progress'
            """,
            (),
        )
        logger.info("health_zombies_cleared")
    except Exception:
        # Table may not have 'in_progress' rows -- that's fine.
        logger.debug("health_zombies_noop")


async def startup_check(
    github: GitHubCLI,
    db: MemoryDBProtocol | None = None,
) -> bool:
    """Run all startup health checks.  Returns True if everything passes.

    If any critical check fails, logs the error and returns False.
    The orchestrator should halt if this returns False.
    """
    logger.info("health_check_start")

    checks: dict[str, bool] = {}

    # State dir (sync, always first)
    checks["state_dir"] = _check_state_dir()

    # Claude CLI
    checks["claude"] = await _check_claude()

    # GitHub CLI
    checks["github"] = await _check_github(github)

    # Memory DB (if provided)
    if db is not None:
        checks["memory_db"] = await _check_memory_db(db)
        await _clear_zombies(db)
    else:
        checks["memory_db"] = True  # Skip if DB not yet initialized

    # GitHub username validation (required for fork, push, duplicate detection)
    if not settings.github_username:
        logger.error("health_github_username_empty",
                     msg="OSBOT_GITHUB_USERNAME must be set. Fork/push/dedup all require it.")
        checks["github_username"] = False
    else:
        checks["github_username"] = True
        logger.debug("health_github_username_ok", username=settings.github_username)

    # OAuth token expiry check (non-blocking -- sends email alert if expiring)
    try:
        from osbot.tokens.probe import check_token_expiry
        await check_token_expiry()
        logger.info("health_token_expiry_checked")
    except Exception as exc:
        logger.warning("health_token_expiry_error", error=str(exc))
        # Non-fatal: token expiry check failure should not block startup

    all_ok = all(checks.values())
    if all_ok:
        logger.info("health_check_passed", checks=checks)
    else:
        failed = [name for name, ok in checks.items() if not ok]
        logger.error("health_check_failed", failed=failed, checks=checks)

        # Send webhook alert for health check failure
        from osbot.comms.webhook import send_alert
        await send_alert(
            f"Health check FAILED: {', '.join(failed)}. Bot will not start.",
            severity="critical",
        )

    return all_ok
