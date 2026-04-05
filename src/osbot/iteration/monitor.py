"""PR monitor -- poll open PRs for new activity via GraphQL.

Each cycle, queries GitHub for all open PRs created by the bot.
Returns ``PRUpdate`` only for PRs with new activity since last check.
No Claude calls -- pure GraphQL polling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from osbot.config import settings
from osbot.log import get_logger
from osbot.types import GitHubCLIProtocol, MemoryDBProtocol, OpenPR

logger = get_logger(__name__)

_PR_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      state
      merged
      mergeable
      comments(first: 100) {
        nodes { author { login } authorAssociation body createdAt }
      }
      reviews(first: 50) {
        nodes {
          author { login } authorAssociation body state createdAt
          comments(first: 50) {
            nodes { author { login } authorAssociation body path line createdAt }
          }
        }
      }
      commits(last: 1) {
        nodes { commit { statusCheckRollup { state } } }
      }
    }
  }
}
"""


@dataclass(frozen=True, slots=True)
class PRUpdate:
    """A PR with new activity requiring action."""

    pr: OpenPR
    new_comments: list[dict[str, Any]] = field(default_factory=list)
    new_reviews: list[dict[str, Any]] = field(default_factory=list)
    ci_status: str = ""
    is_merged: bool = False
    is_closed: bool = False
    has_conflicts: bool = False
    has_new_feedback: bool = False


async def check_prs(
    open_prs: list[OpenPR],
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
) -> list[PRUpdate]:
    """Poll each open PR for new comments, reviews, CI status, merge state.

    Returns only PRs where something changed since ``pr.last_checked_at``.
    """
    updates: list[PRUpdate] = []
    for pr in open_prs:
        try:
            update = await _poll_single_pr(pr, github)
        except Exception as exc:
            logger.warning("pr_poll_failed", repo=pr.repo, pr=pr.pr_number, error=str(exc))
            continue
        if update is not None:
            updates.append(update)
            logger.info(
                "pr_activity_detected", repo=pr.repo, pr=pr.pr_number,
                comments=len(update.new_comments), reviews=len(update.new_reviews),
                ci=update.ci_status, merged=update.is_merged,
            )
    return updates


async def _poll_single_pr(pr: OpenPR, github: GitHubCLIProtocol) -> PRUpdate | None:
    """Query GraphQL for a single PR and detect new activity."""
    owner, name = pr.repo.split("/", 1)
    since = pr.last_checked_at or pr.submitted_at

    try:
        data = await github.graphql(
            _PR_QUERY, variables={"owner": owner, "repo": name, "number": int(pr.pr_number)},
        )
    except RuntimeError as exc:
        logger.warning("pr_graphql_failed", repo=pr.repo, pr=pr.pr_number, error=str(exc))
        return None

    pr_data = data.get("data", {}).get("repository", {}).get("pullRequest")
    if pr_data is None:
        return None

    is_merged = pr_data.get("merged", False)
    is_closed = pr_data.get("state", "").upper() == "CLOSED" and not is_merged
    has_conflicts = pr_data.get("mergeable", "") == "CONFLICTING"

    # Filter comments / reviews newer than last check, excluding our own.
    bot_login = settings.github_username.lower()
    if bot_login:
        new_comments = [
            c for c in pr_data.get("comments", {}).get("nodes", [])
            if c.get("createdAt", "") > since
            and (c.get("author") or {}).get("login", "").lower() != bot_login
        ]
        new_reviews = [
            r for r in pr_data.get("reviews", {}).get("nodes", [])
            if r.get("createdAt", "") > since
            and (r.get("author") or {}).get("login", "").lower() != bot_login
        ]
        # Also strip bot's own inline comments nested inside reviews.
        for r in new_reviews:
            nodes = r.get("comments", {}).get("nodes", [])
            if nodes:
                r["comments"] = {
                    "nodes": [
                        c for c in nodes
                        if (c.get("author") or {}).get("login", "").lower() != bot_login
                    ],
                }
    else:
        # No username configured -- don't filter anything.
        new_comments = [
            c for c in pr_data.get("comments", {}).get("nodes", [])
            if c.get("createdAt", "") > since
        ]
        new_reviews = [
            r for r in pr_data.get("reviews", {}).get("nodes", [])
            if r.get("createdAt", "") > since
        ]

    # CI status from latest commit.
    ci_status = ""
    commits = pr_data.get("commits", {}).get("nodes", [])
    if commits:
        rollup = commits[0].get("commit", {}).get("statusCheckRollup")
        if rollup:
            state = rollup.get("state", "").upper()
            ci_status = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure",
                         "PENDING": "pending"}.get(state, "")

    has_new = bool(new_comments or new_reviews or is_merged or is_closed
                   or has_conflicts or ci_status == "failure")
    if not has_new:
        return None

    return PRUpdate(
        pr=pr, new_comments=new_comments, new_reviews=new_reviews,
        ci_status=ci_status, is_merged=is_merged, is_closed=is_closed,
        has_conflicts=has_conflicts, has_new_feedback=bool(new_comments or new_reviews),
    )
