"""Feedback reader -- Call #4: classify maintainer feedback.

Takes new comments/reviews on a PR and classifies them into one of
six action types.  Extracts action items for actionable feedback,
or the lesson for rejections.  Model: sonnet.  Timeout: 60s.
"""

from __future__ import annotations

import json
from typing import Any

from osbot.config import settings
from osbot.log import get_logger
from osbot.text import truncate
from osbot.types import (
    ClaudeGatewayProtocol,
    FeedbackAction,
    FeedbackResult,
    FeedbackType,
    OpenPR,
    Phase,
    Priority,
)

logger = get_logger(__name__)

_TYPE_MAP: dict[str, FeedbackType] = {
    "request_changes": FeedbackType.REQUEST_CHANGES,
    "style_feedback": FeedbackType.STYLE_FEEDBACK,
    "question": FeedbackType.QUESTION,
    "approval_pending_minor": FeedbackType.APPROVAL_PENDING_MINOR,
    "rejection_with_reason": FeedbackType.REJECTION_WITH_REASON,
    "ci_failure": FeedbackType.CI_FAILURE,
}

_NO_ACTION = FeedbackResult(
    feedback_type=FeedbackType.QUESTION, actions=[],
    should_respond=False, should_patch=False,
)


async def read_feedback(
    pr: OpenPR, comments: list[dict[str, Any]], gateway: ClaudeGatewayProtocol,
) -> FeedbackResult:
    """Classify maintainer feedback and extract action items (Call #4).

    Priority 0 (FEEDBACK_RESPONSE) -- a maintainer waiting is the
    highest-value moment in the system.
    """
    if not comments:
        return _NO_ACTION

    # Defense-in-depth: filter out the bot's own comments even if the
    # caller (monitor.py) already did so.  Prevents self-referencing loops.
    bot_login = settings.github_username.lower()
    if bot_login:
        comments = [
            c for c in comments
            if (c.get("author") or {}).get("login", "").lower() != bot_login
        ]
        if not comments:
            return _NO_ACTION

    formatted = _format_comments(comments)
    prompt = (
        f"Classify this maintainer feedback on PR #{pr.pr_number} in {pr.repo}.\n\n"
        f"FEEDBACK:\n{formatted}\n\n"
        "Classify into exactly one type: request_changes, style_feedback, question, "
        "approval_pending_minor, rejection_with_reason, ci_failure.\n\n"
        'Respond with valid JSON only:\n'
        '{"type": "<type>", "summary": "<one sentence>", '
        '"actions": [{"summary": "<what>", "file_path": "<file or null>", '
        '"line_number": <int or null>, "details": "<specifics>"}], '
        '"is_terminal": <bool>}'
    )

    result = await gateway.invoke(
        prompt, phase=Phase.ITERATE, model=settings.feedback_reader_model,
        allowed_tools=[], cwd="/tmp",
        timeout=settings.feedback_reader_timeout_sec,
        priority=Priority.FEEDBACK_RESPONSE,
        max_turns=1,
    )

    if not result.success:
        logger.warning("feedback_reader_failed", repo=pr.repo, pr=pr.pr_number, error=result.error)
        return FeedbackResult(
            feedback_type=FeedbackType.QUESTION, actions=[],
            should_respond=True, should_patch=False,
        )

    return _parse_result(result.text)


def _format_comments(
    comments: list[dict[str, Any]],
    *,
    max_comments: int = 5,
    max_comment_chars: int = 2000,
) -> str:
    """Format comments into a readable block for the prompt.

    Limits to *max_comments* (most recent) with each body truncated
    to *max_comment_chars* to prevent context window bloat.
    """
    # Keep only the most recent comments
    limited = comments[-max_comments:] if len(comments) > max_comments else comments

    parts: list[str] = []
    if len(comments) > max_comments:
        parts.append(f"[{len(comments) - max_comments} earlier comment(s) omitted]")

    for c in limited:
        author = (c.get("author") or {}).get("login", "unknown")
        assoc = c.get("authorAssociation", "NONE")
        body = c.get("body", "").strip()
        body = truncate(body, max_comment_chars, "comment")
        state = c.get("state", "")
        path = c.get("path", "")
        header = f"@{author} ({assoc})"
        if state:
            header += f" [{state}]"
        if path:
            header += f" on {path}:{c.get('line', '')}"
        parts.append(f"{header}:\n{body}")
    return "\n---\n".join(parts)


def _parse_result(text: str) -> FeedbackResult:
    """Parse Claude's JSON response into a FeedbackResult."""
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(ln for ln in text.split("\n") if not ln.strip().startswith("```"))

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("feedback_parse_failed", text=text[:200])
        return FeedbackResult(
            feedback_type=FeedbackType.QUESTION, actions=[],
            should_respond=True, should_patch=False,
        )

    ft = _TYPE_MAP.get(data.get("type", "question"), FeedbackType.QUESTION)
    actions = [
        FeedbackAction(
            feedback_type=ft, summary=a.get("summary", ""),
            file_path=a.get("file_path"), line_number=a.get("line_number"),
            details=a.get("details", ""),
        )
        for a in data.get("actions", [])
    ]
    should_patch = ft in (
        FeedbackType.REQUEST_CHANGES, FeedbackType.STYLE_FEEDBACK,
        FeedbackType.APPROVAL_PENDING_MINOR, FeedbackType.CI_FAILURE,
    )
    return FeedbackResult(
        feedback_type=ft, actions=actions,
        should_respond=True, should_patch=should_patch,
        is_terminal=data.get("is_terminal", False),
    )
