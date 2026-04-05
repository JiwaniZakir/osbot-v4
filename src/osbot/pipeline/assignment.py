"""Assignment flow -- state machine for repos requiring assignment.

States: ready -> awaiting -> assigned | rejected | timeout.
The claim comment is posted via gh CLI.  Polling is free (GraphQL).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from osbot.config import settings
from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import (
        GitHubCLIProtocol,
        MemoryDBProtocol,
        ScoredIssue,
    )

logger = get_logger(__name__)

# Assignment states
READY = "ready"
AWAITING = "awaiting"
REJECTED = "rejected"


async def check_assignment(
    issue: ScoredIssue,
    db: MemoryDBProtocol,
    github: GitHubCLIProtocol,
) -> str:
    """Determine assignment status for an issue.

    Returns:
        ``"ready"`` -- no assignment required, or already assigned to us.
        ``"awaiting"`` -- assignment requested, waiting for response.
        ``"rejected"`` -- assigned to someone else, or timed out.
    """
    if not issue.requires_assignment:
        return READY

    # Check if we're already assigned
    status = await poll_assignment(issue, github)
    if status == READY:
        return READY
    if status == REJECTED:
        return REJECTED

    # Check if we've already posted a claim and are waiting
    claim_ts = await db.get_repo_fact(issue.repo, f"claim_ts_{issue.number}")
    if claim_ts:
        # Check timeout
        try:
            claimed_at = datetime.fromisoformat(claim_ts)
            elapsed_hours = (datetime.now(UTC) - claimed_at).total_seconds() / 3600
            if elapsed_hours > settings.assignment_timeout_hours:
                logger.info(
                    "assignment_timeout",
                    repo=issue.repo,
                    issue=issue.number,
                    hours=elapsed_hours,
                )
                return REJECTED
        except (ValueError, TypeError):
            pass
        return AWAITING

    # No claim posted yet -- need to request assignment
    return AWAITING


async def request_assignment(
    issue: ScoredIssue,
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
) -> bool:
    """Post a claim comment on the issue and record the timestamp.

    Returns True if the comment was posted successfully.
    """
    comment_body = (
        "I'd like to work on this issue. "
        "I've looked at the codebase and believe I can provide a fix."
    )

    result = await github.run_gh([
        "issue", "comment", str(issue.number),
        "--repo", issue.repo,
        "--body", comment_body,
    ])

    if not result.success:
        logger.warning(
            "assignment_claim_failed",
            repo=issue.repo,
            issue=issue.number,
            stderr=result.stderr[:200],
        )
        return False

    # Record claim timestamp
    now = datetime.now(UTC).isoformat()
    await db.set_repo_fact(
        issue.repo,
        f"claim_ts_{issue.number}",
        now,
        source="assignment_flow",
        confidence=1.0,
    )

    logger.info("assignment_claimed", repo=issue.repo, issue=issue.number)
    return True


async def poll_assignment(
    issue: ScoredIssue,
    github: GitHubCLIProtocol,
) -> str:
    """Check if we've been assigned to the issue.

    Returns:
        ``"ready"`` -- assigned to us.
        ``"awaiting"`` -- not yet assigned (or no assignees).
        ``"rejected"`` -- assigned to someone else.
    """
    username = settings.github_username
    if not username:
        # Can't check assignment without knowing our username
        return READY

    result = await github.run_gh([
        "issue", "view", str(issue.number),
        "--repo", issue.repo,
        "--json", "assignees",
    ])

    if not result.success:
        return AWAITING

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return AWAITING

    assignees = data.get("assignees", [])
    if not assignees:
        return AWAITING

    assignee_logins = [a.get("login", "").lower() for a in assignees]

    if username.lower() in assignee_logins:
        logger.info("assignment_confirmed", repo=issue.repo, issue=issue.number)
        return READY

    # Assigned to someone else
    logger.info(
        "assignment_rejected",
        repo=issue.repo,
        issue=issue.number,
        assignees=assignee_logins,
    )
    return REJECTED
