"""CLA (Contributor Licence Agreement) detection.

Checks whether a repo requires a CLA and whether we can comply.
Layer 2 -- depends on state (repo_facts cache) and intel (GraphQL).
No Claude calls.
"""

from __future__ import annotations

import re

from osbot.log import get_logger
from osbot.types import GitHubCLIProtocol, MemoryDBProtocol

logger = get_logger(__name__)

# Known CLA bot usernames (case-insensitive matching).
_CLA_BOT_USERNAMES: frozenset[str] = frozenset({
    "claassistant",
    "cla-assistant",
    "googlebot",
    "google-cla",
    "mslobot",
    "microsoft-cla",
    "linux-foundation-easycla",
    "easycla",
    "cla-bot",
    "allcontributors",
    "cla-checker",
    "apache-cla",
})

# Keywords in CONTRIBUTING.md that indicate CLA requirement.
_CLA_KEYWORDS: list[re.Pattern[str]] = [
    re.compile(r"contributor\s+license\s+agreement", re.IGNORECASE),
    re.compile(r"\bCLA\b"),
    re.compile(r"sign\s+(the|our|a)\s+CLA", re.IGNORECASE),
    re.compile(r"cla-assistant\.io", re.IGNORECASE),
    re.compile(r"individual\s+contributor\s+license", re.IGNORECASE),
    re.compile(r"apache\s+ICLA", re.IGNORECASE),
]

# Keywords that suggest we cannot auto-sign.
_CANNOT_SIGN_KEYWORDS: list[re.Pattern[str]] = [
    re.compile(r"employer\s+signature", re.IGNORECASE),
    re.compile(r"corporate\s+CLA", re.IGNORECASE),
    re.compile(r"physical\s+signature", re.IGNORECASE),
    re.compile(r"notari[sz]ed", re.IGNORECASE),
]

# Cache key in repo_facts.
_FACT_KEY = "cla_status"


async def check_cla(
    repo: str,
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol | None = None,
) -> tuple[bool, str]:
    """Check if *repo* requires a CLA we cannot satisfy.

    Returns:
        ``(True, "")`` if no CLA is required or we have already signed.
        ``(False, reason)`` if CLA is required and we cannot comply.
    """
    # Check cache first.
    if db is not None:
        cached = await db.get_repo_fact(repo, _FACT_KEY)
        if cached is not None:
            if cached == "not_required":
                return True, ""
            if cached == "signed":
                return True, ""
            return False, f"CLA required: {cached}"

    owner, name = repo.split("/", 1)

    # Strategy 1: Check recent PR comments for CLA bot activity.
    cla_from_comments = await _check_cla_bots(owner, name, github)
    if cla_from_comments is not None:
        status = cla_from_comments
        if db is not None:
            await db.set_repo_fact(repo, _FACT_KEY, status, source="cla_bot_scan")
        if status == "needs_signing":
            return False, "CLA bot detected -- signing required"
        if status == "cannot_sign":
            return False, "CLA requires corporate/physical signature"
        return True, ""

    # Strategy 2: Check CONTRIBUTING.md for CLA language.
    cla_from_docs = await _check_contributing_docs(owner, name, github)
    if cla_from_docs is not None:
        if db is not None:
            await db.set_repo_fact(repo, _FACT_KEY, cla_from_docs, source="contributing_md")
        if cla_from_docs in ("needs_signing", "cannot_sign"):
            return False, f"CLA language found in CONTRIBUTING.md: {cla_from_docs}"
        return True, ""

    # No CLA signals found.
    if db is not None:
        await db.set_repo_fact(repo, _FACT_KEY, "not_required", source="no_signals")

    return True, ""


async def _check_cla_bots(
    owner: str, name: str, github: GitHubCLIProtocol
) -> str | None:
    """Scan recent PR comments for CLA bot usernames.

    Returns ``"needs_signing"`` if a CLA bot is found, else ``None``.
    """
    try:
        data = await github.graphql(
            """
            query($owner: String!, $repo: String!) {
              repository(owner: $owner, name: $repo) {
                pullRequests(last: 10, states: [OPEN, MERGED]) {
                  nodes {
                    comments(first: 20) {
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
        logger.debug("cla_graphql_failed", owner=owner, repo=name)
        return None

    prs = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequests", {})
        .get("nodes", [])
    )

    for pr in prs:
        for comment in pr.get("comments", {}).get("nodes", []):
            author = (comment.get("author") or {}).get("login", "").lower()
            if author in _CLA_BOT_USERNAMES:
                body = comment.get("body", "").lower()
                for pat in _CANNOT_SIGN_KEYWORDS:
                    if pat.search(body):
                        return "cannot_sign"
                return "needs_signing"

    return None


async def _check_contributing_docs(
    owner: str, name: str, github: GitHubCLIProtocol
) -> str | None:
    """Fetch CONTRIBUTING.md and scan for CLA language."""
    for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "CONTRIBUTING.rst"):
        result = await github.run_gh(
            ["api", f"repos/{owner}/{name}/contents/{path}", "--jq", ".content"],
        )
        if not result.success:
            continue

        import base64

        try:
            content = base64.b64decode(result.stdout.strip()).decode("utf-8", errors="replace")
        except Exception:
            continue

        for pat in _CLA_KEYWORDS:
            if pat.search(content):
                for cant in _CANNOT_SIGN_KEYWORDS:
                    if cant.search(content):
                        return "cannot_sign"
                return "needs_signing"

    return None
