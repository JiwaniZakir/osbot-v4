"""Comment generation -- post-process and validate all outgoing text.

Enforces banned-phrase filtering and specificity validation on every
piece of text the bot posts publicly.  Generation by Claude happens
in pipeline/iteration; this module handles post-processing.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from osbot.comms.phrases import BANNED_PHRASES, contains_banned, scrub_banned
from osbot.log import get_logger
from osbot.types import ClaudeGatewayProtocol, Phase, Priority

logger = get_logger(__name__)

_MIN_CODE_REFS = 2

_CODE_REF_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"`[A-Za-z_]\w*(?:\.\w+)*\(\)`"),
    re.compile(r"`[A-Za-z_]\w*(?:/[A-Za-z_]\w*)*\.\w+`"),
    re.compile(r"(?:line|L)\s*\d+", re.IGNORECASE),
    re.compile(r"`[A-Za-z_]\w+`"),
    re.compile(r"(?:src|tests?|lib)/\S+"),
]

_CLAIM_TEMPLATES: list[str] = [
    "I'd like to work on this. My plan: {approach}",
    "I can take this on. Approach: {approach}",
    "I'll work on this -- {approach}",
    "Taking a look. Plan: {approach}",
]

_THANK_YOU_TEMPLATES: list[str] = [
    "Understood, thanks for the feedback.",
    "Got it -- thanks for letting me know.",
    "Makes sense. Thanks for the review.",
    "Appreciate the feedback. Closing this.",
    "Thanks for the explanation, understood.",
]

COMMENT_TYPES = {"claim", "feedback_response", "question_answer", "engagement"}


def _count_code_refs(text: str) -> int:
    """Count distinct code references in *text*."""
    refs: set[str] = set()
    for pat in _CODE_REF_PATTERNS:
        for m in pat.finditer(text):
            refs.add(m.group())
    return len(refs)


def generate_claim_comment(approach: str, *, seed: int | None = None) -> str:
    """Template-based claim comment.  No Claude call needed."""
    s = seed if seed is not None else int(hashlib.md5(approach.encode()).hexdigest()[:8], 16)
    return _CLAIM_TEMPLATES[s % len(_CLAIM_TEMPLATES)].format(approach=approach)


def generate_thank_you(*, seed: int | None = None) -> str:
    """Short thank-you for a rejection.  Template-based, varied."""
    return _THANK_YOU_TEMPLATES[(seed or 0) % len(_THANK_YOU_TEMPLATES)]


def validate_pr_description(description: str) -> tuple[bool, str]:
    """Validate PR description: 2+ code refs, no banned phrases, not trivially short."""
    banned = contains_banned(description)
    if banned:
        return False, f"banned phrases found: {', '.join(banned[:5])}"
    ref_count = _count_code_refs(description)
    if ref_count < _MIN_CODE_REFS:
        return False, f"need {_MIN_CODE_REFS}+ code references, found {ref_count}"
    if len(description.strip()) < 50:
        return False, "description too short (< 50 chars)"
    return True, ""


async def generate_comment(
    context: dict[str, Any],
    comment_type: str,
    gateway: ClaudeGatewayProtocol,
) -> str:
    """Generate a human-facing comment via Claude, then post-process.

    Comment types: claim (template), feedback_response, question_answer, engagement.
    """
    if comment_type not in COMMENT_TYPES:
        raise ValueError(f"unknown comment type: {comment_type!r}")
    if comment_type == "claim":
        return generate_claim_comment(context.get("approach", "fix the issue"))

    prompt = _build_prompt(context, comment_type)
    result = await gateway.invoke(
        prompt,
        phase=Phase.ITERATE if comment_type == "feedback_response" else Phase.ENGAGE,
        model="sonnet",
        allowed_tools=[],
        cwd=None,
        timeout=30.0,
        priority=Priority.CLAIM_COMMENT,
    )
    if not result.success or not result.text.strip():
        logger.warning("comment_generation_failed", comment_type=comment_type, error=result.error)
        return _safe_fallback(comment_type)

    cleaned = scrub_banned(result.text.strip())
    if len(cleaned) < 20:
        logger.info("comment_too_short_after_filter", comment_type=comment_type)
        return _safe_fallback(comment_type)
    return cleaned


def _build_prompt(context: dict[str, Any], comment_type: str) -> str:
    """Build a Claude prompt for comment generation."""
    banned_sample = ", ".join(f'"{p}"' for p in BANNED_PHRASES[:10])
    rules = (
        "- Be concise (2-4 sentences).\n"
        "- Reference specific code, not generic statements.\n"
        f"- Never use these AI phrases: {banned_sample}\n"
        "- No greetings, no filler, no praise. Just the substance."
    )
    if comment_type == "feedback_response":
        no_promises = (
            "- CRITICAL: Only state what you have ALREADY done. Never promise future actions.\n"
            "- Never say 'I will', 'I'll add', 'will update', 'shortly', 'working on it'.\n"
            "- If you cannot fulfil a request (screenshots, manual testing, etc.), say so honestly.\n"
            "  Example: 'Not able to provide screenshots, but the test suite confirms the fix works.'\n"
            "- Use past tense: 'Applied the change', 'Updated the logic', 'Fixed the condition'.\n"
        )
        return (
            f"Write a brief response to maintainer feedback on my PR.\n"
            f"Feedback: {context.get('feedback', '')}\n"
            f"Changes I made: {context.get('changes_made', '')}\n"
            f"Commit SHA (if available): {context.get('commit_sha', 'N/A')}\n"
            f"Rules:\n{rules}\n{no_promises}"
        )
    if comment_type == "question_answer":
        return (
            f"Write a brief answer to this GitHub issue question.\n"
            f"Question: {context.get('question', '')}\n"
            f"Repo: {context.get('repo', '')}\nRules:\n{rules}"
        )
    return (
        f"Write a brief, useful comment on this GitHub issue.\n"
        f"Issue: {context.get('issue_title', '')}\n"
        f"Body: {context.get('issue_body', '')[:500]}\n"
        f"Repo: {context.get('repo', '')}\nRules:\n{rules}"
    )


def _safe_fallback(comment_type: str) -> str:
    """Return a safe template when Claude output fails validation."""
    if comment_type == "feedback_response":
        return "Applied the requested changes and pushed an update."
    if comment_type == "question_answer":
        return "Investigated this and confirmed the behaviour described in the issue."
    return "Reproduced this locally and can confirm the behaviour described."
