"""Assignment requirement detection.

Detects whether a repo requires issue assignment before PR submission.
Detection only -- the actual assignment flow (claim, await, poll)
lives in ``osbot.pipeline``.

Layer 2 -- depends on state (repo_facts cache) and GitHub CLI.
No Claude calls.
"""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING

from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import GitHubCLIProtocol, MemoryDBProtocol

logger = get_logger(__name__)

# Cache key in repo_facts.
_FACT_KEY = "requires_assignment"

# Patterns in CONTRIBUTING.md that indicate assignment is required.
_ASSIGNMENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:please|must|should)\s+(?:ask|request)\s+(?:to\s+be\s+)?assign", re.IGNORECASE),
    re.compile(r"claim\s+(?:the\s+)?issue\s+(?:before|first)", re.IGNORECASE),
    re.compile(r"self[- ]assign", re.IGNORECASE),
    re.compile(r"assigned?\s+(?:to\s+you|before\s+(?:working|starting|submitting))", re.IGNORECASE),
    re.compile(r"wait\s+(?:for|to\s+be)\s+assign", re.IGNORECASE),
    re.compile(r"comment\s+(?:on|to)\s+(?:the\s+)?issue\s+(?:to|before)", re.IGNORECASE),
    re.compile(r"do\s+not\s+(?:start|submit|work)\s+(?:on\s+)?(?:a\s+)?(?:PR|pull)", re.IGNORECASE),
]

# Bot usernames that automate issue assignment.
_ASSIGNMENT_BOT_USERNAMES: frozenset[str] = frozenset({
    "github-actions",
    "ossbot",
    "issue-label-bot",
    "todo-bot",
    "allcontributors",
})

# Patterns in bot comments that indicate automated assignment.
_BOT_ASSIGNMENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"assigned?\s+to\s+@", re.IGNORECASE),
    re.compile(r"you\s+(?:have\s+been|are)\s+assigned", re.IGNORECASE),
    re.compile(r"claim(?:ed|ing)\s+(?:this|the)\s+issue", re.IGNORECASE),
]


async def requires_assignment(
    repo: str,
    db: MemoryDBProtocol,
    github: GitHubCLIProtocol,
) -> bool:
    """Check if *repo* requires issue assignment before PR submission.

    Uses a two-layer strategy:
    1. Check ``repo_facts`` cache.
    2. Fetch ``CONTRIBUTING.md`` and scan for assignment language.
    3. Check recent issue comments for assignment bot activity.

    Results are cached in ``repo_facts`` for future calls.

    Returns:
        ``True`` if the repo requires assignment, ``False`` otherwise.
    """
    # Check cache first.
    cached = await db.get_repo_fact(repo, _FACT_KEY)
    if cached is not None:
        return cached == "true"

    owner, name = repo.split("/", 1)

    # Strategy 1: Scan CONTRIBUTING.md.
    if await _check_contributing_docs(owner, name, github):
        await db.set_repo_fact(repo, _FACT_KEY, "true", source="contributing_md")
        logger.info("assignment_required_from_docs", repo=repo)
        return True

    # Strategy 2: Check recent issue comments for assignment bot patterns.
    if await _check_issue_bot_activity(owner, name, github):
        await db.set_repo_fact(repo, _FACT_KEY, "true", source="bot_activity")
        logger.info("assignment_required_from_bots", repo=repo)
        return True

    # No assignment signals found.
    await db.set_repo_fact(repo, _FACT_KEY, "false", source="no_signals")
    return False


async def _check_contributing_docs(
    owner: str, name: str, github: GitHubCLIProtocol
) -> bool:
    """Fetch CONTRIBUTING.md and scan for assignment language."""
    for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "CONTRIBUTING.rst"):
        result = await github.run_gh(
            ["api", f"repos/{owner}/{name}/contents/{path}", "--jq", ".content"],
        )
        if not result.success:
            continue

        try:
            content = base64.b64decode(result.stdout.strip()).decode("utf-8", errors="replace")
        except Exception:
            continue

        for pattern in _ASSIGNMENT_PATTERNS:
            if pattern.search(content):
                return True

    return False


async def _check_issue_bot_activity(
    owner: str, name: str, github: GitHubCLIProtocol
) -> bool:
    """Check recent issues for assignment bot comments."""
    try:
        data = await github.graphql(
            """
            query($owner: String!, $repo: String!) {
              repository(owner: $owner, name: $repo) {
                issues(last: 10, states: [OPEN, CLOSED]) {
                  nodes {
                    comments(first: 15) {
                      nodes {
                        author { login }
                        body
                      }
                    }
                  }
                }
              }
            }
            """,
            variables={"owner": owner, "repo": name},
        )
    except RuntimeError:
        logger.debug("assignment_graphql_failed", owner=owner, repo=name)
        return False

    issues = (
        data.get("data", {})
        .get("repository", {})
        .get("issues", {})
        .get("nodes", [])
    )

    for issue in issues:
        for comment in issue.get("comments", {}).get("nodes", []):
            author = (comment.get("author") or {}).get("login", "").lower()
            body = comment.get("body", "")

            if author in _ASSIGNMENT_BOT_USERNAMES:
                for pattern in _BOT_ASSIGNMENT_PATTERNS:
                    if pattern.search(body):
                        return True

    return False
