"""Voyager-style skill library -- store successful diffs and inject as few-shot examples.

When a PR is submitted (not yet merged), record the diff as a "skill" indexed by
(language, issue_type, pattern). When a similar issue comes up, inject the most
relevant skill as a concrete example of what a good fix looks like.

Zero Claude calls -- pure storage and retrieval.
"""

from __future__ import annotations

from osbot.learning.lessons import _classify_issue_type
from osbot.log import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Pattern detection -- keyword signatures found in diffs
# ---------------------------------------------------------------------------

# Maps pattern name -> list of substrings to look for in the diff
_DIFF_PATTERNS: dict[str, list[str]] = {
    "null_check":       ["is None", "is not None", "!= None", "== None", "if not ", "Optional"],
    "import_fix":       ["import ", "from ", "ImportError", "ModuleNotFoundError"],
    "type_annotation":  [": int", ": str", ": float", ": bool", ": list", ": dict", "-> None", "-> str", "-> int"],
    "off_by_one":       ["+ 1", "- 1", "<= ", ">= ", "range(", "[:-1]", "[1:]"],
    "key_error":        ["KeyError", ".get(", "in dict", "setdefault"],
    "attribute_error":  ["AttributeError", "hasattr", "getattr"],
    "missing_default":  ["= None", "default=", "or ''", "or []", "or {}"],
    "wrong_comparison": ["== True", "== False", "is True", "is False", "!= ''", "== ''"],
    "encoding_fix":     ["encoding=", "utf-8", "decode(", "encode("],
    "config_fix":       ["config", "settings", ".env", "os.environ", "getenv"],
}


def _detect_pattern(diff: str) -> str:
    """Detect the most prominent fix pattern from the diff content.

    Looks only at added lines (starting with '+') to focus on what was
    actually changed, not what was removed.  Returns the first matching
    pattern name, or "general" if nothing matches.
    """
    # Extract only the added lines for pattern matching
    added_lines = "\n".join(
        line[1:]  # strip the leading '+'
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )

    for pattern_name, keywords in _DIFF_PATTERNS.items():
        if any(kw in added_lines for kw in keywords):
            return pattern_name

    return "general"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def record_skill(
    repo: str,
    issue_number: int,
    title: str,
    labels: list[str],
    language: str,
    diff: str,
    db: object,
) -> None:
    """Record a successful diff as a reusable skill.

    Classifies the issue type and detects the fix pattern from the diff,
    then stores a compressed snippet (first 600 chars) in the skills table.

    Args:
        repo:         Repository slug ("owner/name").
        issue_number: GitHub issue number.
        title:        Issue title.
        labels:       Issue labels.
        language:     Primary programming language of the repo (may be empty).
        diff:         Full unified diff produced by the implementation.
        db:           MemoryDB instance (must have ``record_skill`` method).
    """
    issue_type = _classify_issue_type(title, labels)
    pattern = _detect_pattern(diff)

    # Compress: keep only the first 600 characters
    diff_summary = diff[:600]

    try:
        await db.record_skill(  # type: ignore[attr-defined]
            repo=repo,
            issue_number=issue_number,
            issue_type=issue_type,
            language=language,
            pattern=pattern,
            diff_summary=diff_summary,
            title=title[:100],
        )
        logger.info(
            "skill_recorded",
            repo=repo,
            issue_type=issue_type,
            pattern=pattern,
        )
    except Exception as exc:
        logger.warning("skill_record_failed", repo=repo, issue=issue_number, error=str(exc))


async def get_skill_example(issue: object, language: str, db: object) -> str:
    """Return a few-shot skill example for injection into the implementation prompt.

    Looks up the most relevant skill by (issue_type, language) and formats it
    as a concrete example section.  Returns an empty string if no skill is found
    or if any error occurs (non-critical path).

    Args:
        issue:    ScoredIssue (or any object with ``.title`` and ``.labels``).
        language: Primary language of the repo (may be empty).
        db:       MemoryDB instance (must have ``get_relevant_skills`` method).

    Returns:
        A formatted string section ready for prompt injection, or "".
    """
    try:
        title: str = getattr(issue, "title", "") or ""
        labels: list[str] = list(getattr(issue, "labels", []) or [])
        issue_type = _classify_issue_type(title, labels)

        rows = await db.get_relevant_skills(issue_type, language, limit=1)  # type: ignore[attr-defined]
        if not rows:
            return ""

        row = rows[0]
        skill_issue_type = row.get("issue_type") or issue_type or "unknown"
        skill_pattern = row.get("pattern") or "general"
        diff_summary = row.get("diff_summary") or ""

        if not diff_summary.strip():
            return ""

        return (
            "SKILL EXAMPLE (how a similar fix was done in another repo):\n"
            f"Issue type: {skill_issue_type} | Pattern: {skill_pattern}\n"
            "Diff snippet: \n"
            f"{diff_summary}\n"
            "Note: Your fix should be similarly minimal and focused.\n"
        )
    except Exception:
        return ""  # Skills unavailable -- no problem
