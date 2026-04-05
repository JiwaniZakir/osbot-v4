"""Engage phase -- comment helpfully on issues before contributing.

Picks 1-2 issues from the queue that we haven't engaged on, reads the
issue body, generates a helpful diagnostic comment via Claude (sonnet,
max_turns=1), and posts it.  The comment should demonstrate genuine
understanding of the problem, not just "I'll work on this."

Applies banned phrase filtering and humanizer delays.
Max 2 Claude calls per cycle.
"""

from __future__ import annotations

import json
import random
from datetime import UTC
from typing import TYPE_CHECKING

from osbot.comms.phrases import BANNED_PHRASES, contains_banned, scrub_banned
from osbot.config import settings
from osbot.log import get_logger
from osbot.timing.humanizer import Humanizer
from osbot.types import (
    ClaudeGatewayProtocol,
    GitHubCLIProtocol,
    MemoryDBProtocol,
    Phase,
    Priority,
    ScoredIssue,
)

if TYPE_CHECKING:
    from osbot.state.bot_state import BotState

logger = get_logger(__name__)

_MAX_ENGAGEMENTS_PER_CYCLE = 2
_ENGAGE_TIMEOUT_SEC = 60.0

_humanizer = Humanizer()


async def _ensure_engaged_table(db: MemoryDBProtocol) -> None:
    """Create the engaged_issues tracking table if it does not exist."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS engaged_issues (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            engaged_at TEXT NOT NULL,
            UNIQUE(repo, issue_number)
        )
        """
    )


async def _is_already_engaged(db: MemoryDBProtocol, repo: str, issue_number: int) -> bool:
    """Return True if we already engaged on this issue."""
    row = await db.fetchone(
        "SELECT 1 FROM engaged_issues WHERE repo = ? AND issue_number = ?",
        (repo, issue_number),
    )
    return row is not None


async def _record_engagement(db: MemoryDBProtocol, repo: str, issue_number: int) -> None:
    """Record that we engaged on an issue so we skip it next time."""
    from datetime import datetime

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(
        "INSERT OR IGNORE INTO engaged_issues (repo, issue_number, engaged_at) VALUES (?, ?, ?)",
        (repo, issue_number, now),
    )


async def _has_own_comment(
    github: GitHubCLIProtocol,
    repo: str,
    issue_number: int,
) -> bool:
    """Check whether the bot already commented on this issue via the GitHub API.

    Belt-and-suspenders check on top of the ``engaged_issues`` DB table, which
    could be stale after a DB reset or migration issue.
    """
    bot_login = settings.github_username.lower()
    if not bot_login:
        return False

    result = await github.run_gh([
        "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "comments",
    ])
    if not result.success:
        return False

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return False

    comments = data.get("comments", [])
    if not isinstance(comments, list):
        return False

    for comment in comments:
        author = (comment.get("author") or {}).get("login", "")
        if author.lower() == bot_login:
            return True

    return False


async def _pick_engagement_candidates(
    state: BotState,
    db: MemoryDBProtocol,
) -> list[ScoredIssue]:
    """Pick issues from the queue suitable for engagement.

    Selects issues we haven't already engaged on and don't already have a PR
    or claim on, prioritizing higher-scored ones.  Does NOT pop them from the
    queue -- engagement is non-destructive.
    """
    candidates: list[ScoredIssue] = []

    # Read the current queue and active/open PR repos (snapshot, no mutation)
    async with state._lock:
        queue_snapshot = list(state.issue_queue)
        open_pr_issues = {(pr.repo, pr.issue_number) for pr in state.open_prs}
        active_issues = {
            (w.repo, w.number)
            for w in state.active_work.values()
            if hasattr(w, "repo")
        }

    # Both sets use (repo, issue_number) tuples
    skip_issues = open_pr_issues | active_issues

    for issue in queue_snapshot:
        if await _is_already_engaged(db, issue.repo, issue.number):
            continue
        # Skip issues where we already have an open PR or active work
        if (issue.repo, issue.number) in skip_issues:
            continue
        # Skip issues where we already posted a claim comment
        claim_key = f"claim_ts_{issue.number}"
        claim_row = await db.fetchone(
            "SELECT 1 FROM repo_facts WHERE repo = ? AND key = ?",
            (issue.repo, claim_key),
        )
        if claim_row is not None:
            continue
        candidates.append(issue)

    # Sort by score descending, pick top candidates
    candidates.sort(key=lambda i: i.score, reverse=True)
    return candidates[:_MAX_ENGAGEMENTS_PER_CYCLE * 3]  # extra candidates in case some fail


def _build_engage_prompt(repo: str, issue_title: str, issue_body: str, labels: list[str]) -> str:
    """Build a prompt for Claude to generate a helpful diagnostic comment."""
    body_truncated = issue_body[:2000] if issue_body else "(no body)"
    label_str = ", ".join(labels) if labels else "none"
    banned_sample = ", ".join(f'"{p}"' for p in BANNED_PHRASES[:8])

    return (
        f"You are a developer reading a GitHub issue and want to post a helpful diagnostic comment.\n"
        f"Repo: {repo}\n"
        f"Title: {issue_title}\n"
        f"Labels: {label_str}\n"
        f"Body:\n{body_truncated}\n\n"
        f"Write a brief diagnostic comment (2-4 sentences). Requirements:\n"
        f"- Show you understand the specific problem described\n"
        f"- If possible, suggest a likely root cause (e.g., 'This could be caused by X in Y')\n"
        f"- If the issue mentions an error, comment on what that error typically means\n"
        f"- If you can identify a relevant file or function, mention it\n"
        f"- Do NOT say 'I will work on this' or 'I can fix this' or volunteer to contribute\n"
        f"- Do NOT ask questions that are already answered in the issue body\n"
        f"- Do NOT use greetings, praise, or closing pleasantries\n"
        f"- Never use these phrases: {banned_sample}\n"
        f"- Write as a peer developer sharing a diagnostic insight\n"
        f"- Output ONLY the comment text, no preamble"
    )


async def run_engage_phase(
    state: BotState,
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
    gateway: ClaudeGatewayProtocol,
) -> None:
    """Execute the engage phase: pick issues, generate diagnostic comments, post."""
    logger.info("phase_engage_start")

    try:
        await _ensure_engaged_table(db)
    except Exception as exc:
        logger.error("engage_table_create_error", error=str(exc))
        return

    try:
        candidates = await _pick_engagement_candidates(state, db)
    except Exception as exc:
        logger.error("engage_candidate_error", error=str(exc))
        return

    if not candidates:
        logger.info("phase_engage_done", engagements=0, reason="no candidates")
        return

    # Shuffle to add variety, then pick up to MAX
    if len(candidates) > _MAX_ENGAGEMENTS_PER_CYCLE:
        # Weighted random: prefer higher-scored but allow variety
        candidates = random.sample(
            candidates,
            min(_MAX_ENGAGEMENTS_PER_CYCLE + 1, len(candidates)),
        )
    to_engage = candidates[:_MAX_ENGAGEMENTS_PER_CYCLE]

    engagements_posted = 0
    for issue in to_engage:
        try:
            # Humanizer delay before posting
            await _humanizer.delay_engagement()

            # Build prompt from the issue data we already have
            prompt = _build_engage_prompt(
                repo=issue.repo,
                issue_title=issue.title,
                issue_body=issue.body,
                labels=issue.labels,
            )

            # Call Claude to generate the diagnostic comment
            result = await gateway.invoke(
                prompt,
                phase=Phase.ENGAGE,
                model="sonnet",
                allowed_tools=[],
                cwd=None,
                timeout=_ENGAGE_TIMEOUT_SEC,
                priority=Priority.CLAIM_COMMENT,
                max_turns=1,
            )

            if not result.success or not result.text.strip():
                logger.warning(
                    "engage_generation_failed",
                    repo=issue.repo,
                    issue=issue.number,
                    error=result.error,
                )
                continue

            # Scrub banned phrases
            comment_text = scrub_banned(result.text.strip())

            # Validate substance after scrubbing
            if len(comment_text) < 30:
                logger.info(
                    "engage_too_short_after_scrub",
                    repo=issue.repo,
                    issue=issue.number,
                )
                continue

            # Double-check no banned phrases remain
            remaining = contains_banned(comment_text)
            if remaining:
                logger.warning(
                    "engage_banned_phrases_after_scrub",
                    repo=issue.repo,
                    issue=issue.number,
                    phrases=remaining[:3],
                )
                continue

            # Belt-and-suspenders: verify via GitHub API that we haven't
            # already commented on this issue (covers stale DB scenarios).
            try:
                if await _has_own_comment(github, issue.repo, issue.number):
                    # Record in engaged_issues so we don't re-check next cycle
                    await _record_engagement(db, issue.repo, issue.number)
                    logger.info(
                        "engage_skip_already_commented",
                        repo=issue.repo,
                        issue=issue.number,
                    )
                    continue
            except Exception as exc:
                # Non-fatal: if the check fails, proceed with the DB-based guard
                logger.debug(
                    "engage_own_comment_check_error",
                    repo=issue.repo,
                    issue=issue.number,
                    error=str(exc),
                )

            # Post the comment
            post_result = await github.run_gh([
                "issue", "comment", str(issue.number),
                "--repo", issue.repo,
                "--body", comment_text,
            ])

            if post_result.success:
                await _record_engagement(db, issue.repo, issue.number)
                engagements_posted += 1
                logger.info(
                    "engage_posted",
                    repo=issue.repo,
                    issue=issue.number,
                    comment_len=len(comment_text),
                )
            else:
                logger.warning(
                    "engage_post_failed",
                    repo=issue.repo,
                    issue=issue.number,
                    stderr=post_result.stderr[:200],
                )

        except Exception as exc:
            logger.error(
                "engage_error",
                repo=issue.repo,
                issue=issue.number,
                error=str(exc),
                exc_info=True,
            )

    logger.info("phase_engage_done", engagements=engagements_posted)
