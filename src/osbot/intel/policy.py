"""Policy reader -- parse CONTRIBUTING.md for contribution requirements.

Extracts commit format, branch strategy, PR template requirements,
test requirements, assignment requirements, and AI policy stance.

Zero Claude calls.  Layer 2.
"""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING, Any

from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import GitHubCLIProtocol

logger = get_logger(__name__)

# Paths to check for contributing guidelines (in priority order).
_CONTRIBUTING_PATHS: list[str] = [
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
]

# Patterns that indicate assignment is required before working.
_ASSIGNMENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(ask|request)\s+(to\s+be\s+)?assign", re.IGNORECASE),
    re.compile(r"please\s+(comment|ask)\s+(on\s+the\s+issue\s+)?(before|first)", re.IGNORECASE),
    re.compile(r"do\s+not\s+(start|begin|work)\s+.*?without\s+(being\s+)?assign", re.IGNORECASE),
    re.compile(r"claim\s+(the\s+)?issue\s+(before|first)", re.IGNORECASE),
]

# Patterns for commit message format requirements.
_COMMIT_FORMAT_PATTERNS: dict[str, re.Pattern[str]] = {
    "conventional": re.compile(
        r"conventional\s+commit|feat:|fix:|chore:|docs:|refactor:", re.IGNORECASE
    ),
    "signed-off": re.compile(r"signed-off-by|DCO|Developer\s+Certificate", re.IGNORECASE),
    "issue-ref": re.compile(r"(reference|include|mention)\s+(the\s+)?(issue|ticket)", re.IGNORECASE),
}

# Patterns for branch naming conventions.
_BRANCH_PATTERNS: dict[str, re.Pattern[str]] = {
    "feature-prefix": re.compile(r"(feature|fix|bugfix)/", re.IGNORECASE),
    "issue-number": re.compile(r"branch.*issue.*number|branch.*#\d+", re.IGNORECASE),
}

# Patterns for test requirements.
_TEST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(run|add|include|write)\s+(the\s+)?tests?", re.IGNORECASE),
    re.compile(r"pytest|npm\s+test|yarn\s+test|make\s+test", re.IGNORECASE),
    re.compile(r"all\s+tests?\s+(must|should|need\s+to)\s+pass", re.IGNORECASE),
]

# No-AI / no-bot policy patterns (same as safety/domain.py for consistency).
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


async def read_policy(
    repo: str,
    github: GitHubCLIProtocol,
) -> dict[str, Any]:
    """Fetch and parse CONTRIBUTING.md, extracting contribution requirements.

    Args:
        repo: ``"owner/name"`` identifier.
        github: CLI protocol for running ``gh`` commands.

    Returns:
        Dict with keys: ``requires_assignment``, ``commit_format``,
        ``branch_strategy``, ``test_requirements``, ``has_pr_template``,
        ``has_ai_policy``, ``raw_text`` (first 4000 chars of CONTRIBUTING.md).
    """
    result: dict[str, Any] = {
        "requires_assignment": False,
        "commit_format": None,
        "branch_strategy": None,
        "test_requirements": False,
        "has_pr_template": False,
        "has_ai_policy": False,
        "raw_text": "",
    }

    # Try to find a CONTRIBUTING file
    contributing_text = ""
    for path in _CONTRIBUTING_PATHS:
        content_result = await github.run_gh([
            "api", f"repos/{repo}/contents/{path}", "--jq", ".content",
        ])
        if content_result.success and content_result.stdout.strip():
            decoded = _decode_base64(content_result.stdout.strip())
            if decoded:
                contributing_text = decoded
                break

    if contributing_text:
        result["raw_text"] = contributing_text[:4000]
        result["requires_assignment"] = _detect_assignment(contributing_text)
        result["commit_format"] = _detect_commit_format(contributing_text)
        result["branch_strategy"] = _detect_branch_strategy(contributing_text)
        result["test_requirements"] = _detect_test_requirements(contributing_text)
        result["has_ai_policy"] = detect_ai_policy(contributing_text)

    # Check for PR template and fetch its content
    pr_template_text = await fetch_pr_template(repo, github)
    if pr_template_text:
        result["has_pr_template"] = True
        result["pr_template_text"] = pr_template_text

    logger.info(
        "policy_read",
        repo=repo,
        requires_assignment=result["requires_assignment"],
        commit_format=result["commit_format"],
        has_ai_policy=result["has_ai_policy"],
    )
    return result


async def fetch_pr_template(
    repo: str,
    github: GitHubCLIProtocol,
) -> str | None:
    """Fetch the PR template content from the repo, if one exists.

    Checks common locations in priority order and returns the decoded
    template text, or ``None`` if no template is found.

    Args:
        repo: ``"owner/name"`` identifier.
        github: CLI protocol for running ``gh`` commands.

    Returns:
        The raw template text, or ``None``.
    """
    for template_path in (
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/pull_request_template.md",
        "PULL_REQUEST_TEMPLATE.md",
        "pull_request_template.md",
        "docs/PULL_REQUEST_TEMPLATE.md",
    ):
        template_result = await github.run_gh([
            "api", f"repos/{repo}/contents/{template_path}", "--jq", ".content",
        ])
        if template_result.success and template_result.stdout.strip():
            decoded = _decode_base64(template_result.stdout.strip())
            if decoded:
                logger.info(
                    "pr_template_found",
                    repo=repo,
                    path=template_path,
                    length=len(decoded),
                )
                return decoded
    return None


def detect_ai_policy(contributing_text: str) -> bool:
    """Return True if text contains indicators of a no-AI contribution policy.

    Args:
        contributing_text: Raw text content of CONTRIBUTING.md.
    """
    return any(pattern.search(contributing_text) for pattern in _NO_AI_PATTERNS)


def _detect_assignment(text: str) -> bool:
    """Return True if the text requires assignment before working."""
    return any(pattern.search(text) for pattern in _ASSIGNMENT_PATTERNS)


def _detect_commit_format(text: str) -> str | None:
    """Return the commit format name if detected, else None."""
    for name, pattern in _COMMIT_FORMAT_PATTERNS.items():
        if pattern.search(text):
            return name
    return None


def _detect_branch_strategy(text: str) -> str | None:
    """Return the branch naming convention if detected, else None."""
    for name, pattern in _BRANCH_PATTERNS.items():
        if pattern.search(text):
            return name
    return None


def _detect_test_requirements(text: str) -> bool:
    """Return True if contributing guidelines mention test requirements."""
    return any(pattern.search(text) for pattern in _TEST_PATTERNS)


def _decode_base64(encoded: str) -> str | None:
    """Decode base64 content from GitHub API, returning None on failure."""
    try:
        cleaned = encoded.replace("\n", "").replace("\\n", "")
        return base64.b64decode(cleaned).decode("utf-8", errors="replace")
    except Exception:
        return None
