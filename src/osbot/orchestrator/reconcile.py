"""Startup reconciliation — adopt orphan PRs.

If the container is terminated between `gh pr create` (PR exists on GitHub)
and `state.add_open_pr` (PR recorded in state.json), the PR becomes an
orphan: visible on GitHub, invisible to the iteration phase. Feedback gets
no response, maintainers eventually close the PR. This module fixes that
on every startup.

At boot:

1. Query GitHub for all open PRs authored by the bot account.
2. Diff against `state.open_prs`.
3. For every PR on GitHub but missing locally, add it to `open_prs` so the
   iteration phase picks it up next cycle.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from osbot.config import settings
from osbot.log import get_logger
from osbot.types import OpenPR

if TYPE_CHECKING:
    from osbot.state import BotState

logger = get_logger(__name__)


async def reconcile_open_prs(state: BotState) -> int:
    """Adopt any bot-authored open PRs that aren't in state.open_prs.

    Returns the number of orphans adopted. Uses `gh` via
    `asyncio.create_subprocess_exec` (no shell interpolation).
    """
    username = settings.github_username
    if not username:
        logger.warning("reconcile_no_username")
        return 0

    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "search",
            "prs",
            "--author",
            username,
            "--state",
            "open",
            "--limit",
            "200",
            # `gh search prs` doesn't expose headRefName; we default the branch
            # to a synthetic fix/{number} placeholder — the iteration phase
            # uses repo + pr_number, branch is informational only.
            "--json",
            "number,repository,createdAt",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (TimeoutError, OSError) as exc:
        logger.warning("reconcile_gh_error", error=str(exc))
        return 0

    if proc.returncode != 0:
        logger.warning("reconcile_gh_failed", stderr=stderr.decode(errors="replace")[:200])
        return 0

    try:
        gh_prs = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        logger.warning("reconcile_parse_error", error=str(exc))
        return 0

    existing = {(p.repo, p.pr_number) for p in await state.get_open_prs()}
    adopted = 0
    for pr in gh_prs:
        repo = pr.get("repository", {}).get("nameWithOwner", "")
        number = pr.get("number")
        if not repo or not number:
            continue
        # Skip PRs on our own repos (osbot maintenance, not bot contributions).
        if repo.startswith(f"{username}/"):
            continue
        if (repo, number) in existing:
            continue

        branch = f"fix/{number}"
        submitted_at = pr.get("createdAt", datetime.now(UTC).isoformat())
        await state.add_open_pr(
            OpenPR(
                repo=repo,
                issue_number=0,
                pr_number=number,
                url=f"https://github.com/{repo}/pull/{number}",
                branch=branch,
                submitted_at=submitted_at,
            )
        )
        adopted += 1
        logger.info("reconcile_adopted_orphan", repo=repo, pr_number=number)

    if adopted:
        logger.info("reconcile_done", adopted=adopted, gh_total=len(gh_prs))
    else:
        logger.debug("reconcile_done", adopted=0, gh_total=len(gh_prs))
    return adopted
