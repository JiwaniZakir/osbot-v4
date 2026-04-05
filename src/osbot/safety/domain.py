"""Domain enforcement -- keeps the bot in AI/ML Python/TypeScript repos.

Checks language + topic match.  Also detects no-AI policies in
CONTRIBUTING.md to auto-exclude repos that prohibit automated PRs.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from osbot.config import settings
from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import GitHubCLIProtocol, MemoryDBProtocol, RepoMeta

logger = get_logger(__name__)

ALLOWED_LANGUAGES: list[str] = settings.allowed_languages
DOMAIN_KEYWORDS: list[str] = settings.domain_keywords

# Patterns that indicate a no-AI / no-bot policy.
_NO_AI_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"no\s+(ai|automated|bot)\s+(contributions?|pull\s*requests?|prs?)", re.IGNORECASE),
    re.compile(r"do\s+not\s+(submit|send)\s+(ai|automated|bot)", re.IGNORECASE),
    re.compile(
        r"(ai|automated|bot)\s+(contributions?|pull\s*requests?|prs?)\s+are\s+not\s+(accepted|allowed|welcome)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(ai|llm|gpt|copilot|chatgpt).generated\s+(code|contributions?|prs?)\s+(are\s+)?(not\s+)?(accepted|allowed|prohibited|banned)",
        re.IGNORECASE,
    ),
    re.compile(r"must\s+be\s+(written|authored)\s+by\s+(a\s+)?human", re.IGNORECASE),
]


def is_in_domain(repo_meta: RepoMeta) -> bool:
    """Return True if *repo_meta* matches our language AND topic filters.

    Both conditions must be met:
    1. The repo's primary language is in ``ALLOWED_LANGUAGES`` (case-insensitive).
    2. At least one of the repo's topics matches a ``DOMAIN_KEYWORDS`` entry.
    """
    # Language check
    if not repo_meta.language:
        logger.debug("domain_reject", repo=repo_meta.full_name, reason="no_language")
        return False

    if repo_meta.language.lower() not in {lang.lower() for lang in ALLOWED_LANGUAGES}:
        logger.debug(
            "domain_reject",
            repo=repo_meta.full_name,
            reason="language_mismatch",
            language=repo_meta.language,
        )
        return False

    # Topic check
    lower_topics = {t.lower() for t in repo_meta.topics}
    lower_keywords = {kw.lower() for kw in DOMAIN_KEYWORDS}

    if not lower_topics & lower_keywords:
        logger.debug(
            "domain_reject",
            repo=repo_meta.full_name,
            reason="no_domain_keyword",
            topics=repo_meta.topics,
        )
        return False

    return True


async def has_ai_policy(
    repo: str,
    db: MemoryDBProtocol,
    github: GitHubCLIProtocol,
) -> bool:
    """Return True if the repo has a no-AI / no-bot policy.

    Checks cached ``repo_signals.has_ai_policy`` first.  On cache miss,
    fetches CONTRIBUTING.md via ``gh`` and scans for policy patterns.
    """
    # Check cache first
    cached = await db.fetchone(
        "SELECT has_ai_policy FROM repo_signals WHERE repo = ? AND expires_at > datetime('now')",
        (repo,),
    )
    if cached is not None:
        return bool(cached.get("has_ai_policy", 0))

    # Fetch CONTRIBUTING.md via gh api
    result = await github.run_gh(
        ["api", f"repos/{repo}/contents/CONTRIBUTING.md", "--jq", ".content"],
    )

    if not result.success:
        # No CONTRIBUTING.md or API error -- assume no policy
        logger.debug("ai_policy_check", repo=repo, result="no_contributing_md")
        return False

    # gh api returns base64-encoded content; decode
    import base64

    try:
        content = base64.b64decode(result.stdout.strip()).decode("utf-8", errors="replace")
    except Exception:
        logger.warning("ai_policy_check", repo=repo, result="decode_error")
        return False

    # Scan for no-AI patterns
    for pattern in _NO_AI_PATTERNS:
        if pattern.search(content):
            logger.info("ai_policy_detected", repo=repo, pattern=pattern.pattern)
            return True

    return False
