"""Duplicate detector -- check if a PR already exists for a given issue.

Uses GraphQL timeline events (CrossReferencedEvent linking to PRs),
searches for our own open PRs to prevent submitting duplicates, and
checks issue comments for claim language from other contributors.

Zero Claude calls.  Layer 2.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from osbot.config import settings
from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import GitHubCLIProtocol

logger = get_logger(__name__)

# Patterns indicating another contributor has claimed the issue.
# Each pattern is compiled as case-insensitive.
_CLAIM_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bi(?:'m| am) working on this\b", re.IGNORECASE),
    re.compile(r"\bi(?:'ll| will) submit a pr\b", re.IGNORECASE),
    re.compile(r"\bi(?:'ll| will) open a pr\b", re.IGNORECASE),
    re.compile(r"\bi(?:'ll| will) send a pr\b", re.IGNORECASE),
    re.compile(r"\bi(?:'ll| will) fix this\b", re.IGNORECASE),
    re.compile(r"\bi(?:'ll| will) take this\b", re.IGNORECASE),
    re.compile(r"\bi(?:'d| would) like to work on this\b", re.IGNORECASE),
    re.compile(r"\bcan i work on this\b", re.IGNORECASE),
    re.compile(r"\bworking on a fix\b", re.IGNORECASE),
    re.compile(r"\bclaimed\b", re.IGNORECASE),
    re.compile(r"\bwip\b", re.IGNORECASE),
    re.compile(r"\bwork in progress\b", re.IGNORECASE),
    re.compile(r"\btaking this\b", re.IGNORECASE),
    re.compile(r"\bi(?:'ll| will) handle this\b", re.IGNORECASE),
    re.compile(r"\blet me take this\b", re.IGNORECASE),
    re.compile(r"\bpr incoming\b", re.IGNORECASE),
    re.compile(r"\bpr coming\b", re.IGNORECASE),
]

# Max age of a claim comment to still count (prevents stale claims blocking us).
_CLAIM_MAX_AGE_DAYS = 7


async def detect_duplicates(
    repo: str,
    issue_number: int,
    github: GitHubCLIProtocol,
) -> bool:
    """Return True if there is already an open PR addressing this issue.

    Checks two sources:
    1. The issue's GraphQL timeline for ``CrossReferencedEvent`` linking
       to open pull requests.
    2. Our own open PRs (by bot username) mentioning this issue number.

    Args:
        repo: ``"owner/name"`` identifier.
        issue_number: The issue number to check.
        github: CLI protocol for GraphQL and ``gh`` commands.

    Returns:
        True if a duplicate open PR exists, False otherwise.
    """
    owner, name = repo.split("/", 1)

    # 1. Check issue timeline for linked open PRs
    if await _check_timeline(owner, name, issue_number, github):
        return True

    # 2. Check our own open PRs for this repo
    return bool(await _check_own_prs(repo, issue_number, github))


async def _check_timeline(
    owner: str, name: str, issue_number: int, github: GitHubCLIProtocol
) -> bool:
    """Check if the issue timeline has cross-references to open PRs."""
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) {
          timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT]) {
            nodes {
              ... on CrossReferencedEvent {
                source {
                  ... on PullRequest {
                    number
                    state
                    author { login }
                    url
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    try:
        data = await github.graphql(
            query,
            variables={"owner": owner, "repo": name, "number": str(issue_number)},
        )
    except RuntimeError:
        logger.warning("duplicate_check_graphql_failed", repo=f"{owner}/{name}", issue=issue_number)
        return False

    issue = data.get("data", {}).get("repository", {}).get("issue", {})
    if not issue:
        return False

    nodes = issue.get("timelineItems", {}).get("nodes", [])
    for node in nodes:
        source = node.get("source", {})
        if not source:
            continue
        pr_state = source.get("state", "")
        if pr_state == "OPEN":
            pr_number = source.get("number", 0)
            pr_author = source.get("author", {}).get("login", "")
            pr_url = source.get("url", "")
            logger.info(
                "duplicate_found_timeline",
                repo=f"{owner}/{name}",
                issue=issue_number,
                pr_number=pr_number,
                pr_author=pr_author,
                pr_url=pr_url,
            )
            return True

    return False


async def _check_own_prs(
    repo: str, issue_number: int, github: GitHubCLIProtocol
) -> bool:
    """Check if the bot already has an open PR for this issue."""
    bot_username = settings.github_username
    if not bot_username:
        return False

    result = await github.run_gh([
        "pr", "list",
        "--repo", repo,
        "--author", bot_username,
        "--state", "open",
        "--json", "number,title,body,url",
        "--limit", "50",
    ])
    if not result.success:
        return False

    import json

    try:
        prs: list[dict[str, Any]] = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return False

    issue_ref = f"#{issue_number}"
    for pr in prs:
        title = pr.get("title", "")
        body = pr.get("body", "")
        if issue_ref in title or issue_ref in body:
            logger.info(
                "duplicate_found_own_pr",
                repo=repo,
                issue=issue_number,
                pr_number=pr.get("number"),
                pr_url=pr.get("url", ""),
            )
            return True

    return False


def check_claimed_in_comments(
    comments: list[dict[str, Any]],
    bot_username: str = "",
) -> tuple[bool, str]:
    """Check if any recent comment indicates someone has claimed the issue.

    Scans issue comments for claim language (e.g. "I'm working on this",
    "I'll submit a PR", "WIP", "claimed") posted within the last 7 days
    by someone other than the bot itself.

    Args:
        comments: List of comment dicts with ``body``, ``author``,
                  and ``createdAt`` fields (from GraphQL issue_detail).
        bot_username: The bot's GitHub username to exclude from claim
                      detection (we don't block on our own claims).

    Returns:
        ``(True, claimer_login)`` if claimed, ``(False, "")`` otherwise.
    """
    if not comments:
        return False, ""

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=_CLAIM_MAX_AGE_DAYS)
    effective_bot = (bot_username or settings.github_username or "").lower()

    for comment in comments:
        # Skip our own comments
        author_login = (
            comment.get("author", {}).get("login", "")
            if isinstance(comment.get("author"), dict)
            else ""
        ).lower()
        if effective_bot and author_login == effective_bot:
            continue

        # Skip old comments
        created_str = comment.get("createdAt", "")
        if created_str:
            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                if created < cutoff:
                    continue
            except (ValueError, TypeError):
                pass

        body = comment.get("body", "") or ""
        for pattern in _CLAIM_PATTERNS:
            if pattern.search(body):
                logger.info(
                    "issue_claimed_in_comment",
                    author=author_login,
                    pattern=pattern.pattern,
                    body_preview=body[:120],
                )
                return True, author_login

    return False, ""
