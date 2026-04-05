"""Review phase -- review others' open PRs to build reputation.

Searches repos from the active pool for open PRs, reads diffs, generates
a genuine code review via Claude (sonnet, max_turns=1), and posts it.
Tracks reviewed PRs in memory.db to avoid re-reviewing.

Max 2 Claude calls per cycle.
"""

from __future__ import annotations

import json
import random
from datetime import UTC
from typing import Any

from osbot.comms.phrases import contains_banned, scrub_banned
from osbot.config import settings
from osbot.log import get_logger
from osbot.timing.humanizer import Humanizer
from osbot.types import (
    ClaudeGatewayProtocol,
    GitHubCLIProtocol,
    MemoryDBProtocol,
    Phase,
    Priority,
)

logger = get_logger(__name__)

_MAX_REVIEWS_PER_CYCLE = 2
_DIFF_TRUNCATE_CHARS = 8000
_REVIEW_TIMEOUT_SEC = 60.0

_humanizer = Humanizer()


async def _ensure_reviewed_table(db: MemoryDBProtocol) -> None:
    """Create the reviewed_prs tracking table if it does not exist."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS reviewed_prs (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            reviewed_at TEXT NOT NULL,
            UNIQUE(repo, pr_number)
        )
        """
    )


async def _is_already_reviewed(db: MemoryDBProtocol, repo: str, pr_number: int) -> bool:
    """Return True if we already reviewed this PR."""
    row = await db.fetchone(
        "SELECT 1 FROM reviewed_prs WHERE repo = ? AND pr_number = ?",
        (repo, pr_number),
    )
    return row is not None


async def _record_review(db: MemoryDBProtocol, repo: str, pr_number: int) -> None:
    """Record that we reviewed a PR so we skip it next time."""
    from datetime import datetime

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(
        "INSERT OR IGNORE INTO reviewed_prs (repo, pr_number, reviewed_at) VALUES (?, ?, ?)",
        (repo, pr_number, now),
    )


async def _has_own_review(
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
    repo: str,
    pr_number: int,
) -> bool:
    """Check whether the bot already reviewed this PR via the GitHub API.

    Belt-and-suspenders check on top of the ``reviewed_prs`` DB table.  If a
    review by the bot is found on GitHub but not in the DB, the DB is back-filled
    so we don't re-check on every cycle.
    """
    bot_login = settings.github_username.lower()
    if not bot_login:
        return False

    # Check PR review comments (posted via `gh pr review --comment`)
    result = await github.run_gh([
        "api",
        f"repos/{repo}/pulls/{pr_number}/reviews",
        "--method", "GET",
    ])
    if not result.success:
        return False

    try:
        reviews = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return False

    if not isinstance(reviews, list):
        return False

    for review in reviews:
        author = (review.get("user") or {}).get("login", "")
        if author.lower() == bot_login:
            # Back-fill the DB so the fast path catches it next cycle
            await _record_review(db, repo, pr_number)
            return True

    return False


async def _find_candidate_prs(
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
) -> list[dict[str, Any]]:
    """Search repos in repo_signals for open PRs we haven't reviewed.

    Returns a list of dicts with keys: repo, number, title.
    """
    # Get repos from the active pool (repo_signals table)
    rows = await db.fetchall(
        "SELECT repo FROM repo_signals WHERE expires_at > datetime('now') ORDER BY RANDOM() LIMIT 20"
    )
    if not rows:
        return []

    candidates: list[dict[str, Any]] = []
    repos_checked = 0

    for row in rows:
        if repos_checked >= 10:  # Don't check too many repos per cycle
            break
        repo = row["repo"]
        repos_checked += 1

        result = await github.run_gh([
            "pr", "list",
            "--repo", repo,
            "--state", "open",
            "--limit", "5",
            "--json", "number,title,author",
        ])
        if not result.success:
            continue

        try:
            prs = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            continue

        for pr in prs:
            pr_number = pr.get("number")
            if pr_number is None:
                continue

            # Skip our own PRs
            author_login = (pr.get("author") or {}).get("login", "")
            if author_login.lower() in ("jiwanizakir", settings.github_username.lower()):
                continue

            # Skip already-reviewed PRs
            if await _is_already_reviewed(db, repo, pr_number):
                continue

            candidates.append({
                "repo": repo,
                "number": pr_number,
                "title": pr.get("title", ""),
            })

        if len(candidates) >= _MAX_REVIEWS_PER_CYCLE * 2:
            break

    return candidates


async def _get_pr_diff(github: GitHubCLIProtocol, repo: str, pr_number: int) -> str:
    """Fetch the diff for a PR, truncated to a reasonable size."""
    result = await github.run_gh([
        "pr", "diff", str(pr_number),
        "--repo", repo,
    ])
    if not result.success:
        return ""
    diff = result.stdout
    if len(diff) > _DIFF_TRUNCATE_CHARS:
        diff = diff[:_DIFF_TRUNCATE_CHARS] + "\n\n[... diff truncated ...]"
    return diff


def _build_review_prompt(repo: str, pr_title: str, diff: str) -> str:
    """Build a prompt for Claude to generate a helpful code review."""
    return (
        f"You are reviewing a pull request on GitHub.\n"
        f"Repo: {repo}\n"
        f"PR title: {pr_title}\n\n"
        f"Diff:\n```\n{diff}\n```\n\n"
        f"Write a brief, helpful code review comment (3-6 sentences). Requirements:\n"
        f"- Reference specific files, functions, or line changes from the diff\n"
        f"- Point out a concrete observation: a potential edge case, a style issue, "
        f"a missing test scenario, an optimization, or something done well\n"
        f"- Be constructive and specific, not generic praise\n"
        f"- Do NOT use any of these phrases: 'Great job', 'Looks good to me', "
        f"'LGTM', 'Nice work', 'I\'d be happy to', 'Great catch'\n"
        f"- Do NOT start with greetings or praise\n"
        f"- Do NOT offer to help further or add closing pleasantries\n"
        f"- Write as a peer developer giving a careful review\n"
        f"- Output ONLY the review text, no preamble"
    )


async def run_review_phase(
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
    gateway: ClaudeGatewayProtocol,
) -> None:
    """Execute the review phase: find PRs, review 1-2, post comments."""
    logger.info("phase_review_start")

    try:
        await _ensure_reviewed_table(db)
    except Exception as exc:
        logger.error("review_table_create_error", error=str(exc))
        return

    try:
        candidates = await _find_candidate_prs(github, db)
    except Exception as exc:
        logger.error("review_candidate_search_error", error=str(exc))
        return

    if not candidates:
        logger.info("phase_review_done", reviews=0, reason="no candidates")
        return

    # Pick up to MAX_REVIEWS_PER_CYCLE
    random.shuffle(candidates)
    to_review = candidates[:_MAX_REVIEWS_PER_CYCLE]

    reviews_posted = 0
    for pr_info in to_review:
        repo = pr_info["repo"]
        pr_number = pr_info["number"]
        pr_title = pr_info["title"]

        try:
            # Fetch the diff
            diff = await _get_pr_diff(github, repo, pr_number)
            if not diff.strip():
                logger.debug("review_skip_empty_diff", repo=repo, pr=pr_number)
                continue

            # Humanizer delay before generating review
            await _humanizer.delay_engagement()

            # Call Claude to generate review
            prompt = _build_review_prompt(repo, pr_title, diff)
            result = await gateway.invoke(
                prompt,
                phase=Phase.REVIEW,
                model="sonnet",
                allowed_tools=[],
                cwd=None,
                timeout=_REVIEW_TIMEOUT_SEC,
                priority=Priority.DIAGNOSTIC,
                max_turns=1,
            )

            if not result.success or not result.text.strip():
                logger.warning(
                    "review_generation_failed",
                    repo=repo,
                    pr=pr_number,
                    error=result.error,
                )
                continue

            # Scrub banned phrases
            review_text = scrub_banned(result.text.strip())

            # Validate it's not empty after scrubbing and has substance
            if len(review_text) < 30:
                logger.info("review_too_short_after_scrub", repo=repo, pr=pr_number)
                continue

            # Double-check no banned phrases remain
            remaining = contains_banned(review_text)
            if remaining:
                logger.warning(
                    "review_banned_phrases_after_scrub",
                    repo=repo,
                    pr=pr_number,
                    phrases=remaining[:3],
                )
                continue

            # Belt-and-suspenders: verify via GitHub API that we haven't
            # already reviewed this PR (covers stale DB scenarios).
            try:
                if await _has_own_review(github, db, repo, pr_number):
                    logger.info(
                        "review_skip_already_reviewed",
                        repo=repo,
                        pr=pr_number,
                    )
                    continue
            except Exception as exc:
                # Non-fatal: if the check fails, proceed with the DB-based guard
                logger.debug(
                    "review_own_review_check_error",
                    repo=repo,
                    pr=pr_number,
                    error=str(exc),
                )

            # Post the review
            post_result = await github.run_gh([
                "pr", "review", str(pr_number),
                "--repo", repo,
                "--comment",
                "--body", review_text,
            ])

            if post_result.success:
                await _record_review(db, repo, pr_number)
                reviews_posted += 1
                logger.info(
                    "review_posted",
                    repo=repo,
                    pr=pr_number,
                    review_len=len(review_text),
                )
            else:
                logger.warning(
                    "review_post_failed",
                    repo=repo,
                    pr=pr_number,
                    stderr=post_result.stderr[:200],
                )

        except Exception as exc:
            logger.error(
                "review_error",
                repo=repo,
                pr=pr_number,
                error=str(exc),
                exc_info=True,
            )

    logger.info("phase_review_done", reviews=reviews_posted)
