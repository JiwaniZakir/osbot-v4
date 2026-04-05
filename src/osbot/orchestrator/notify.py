"""Notify phase -- respond to @mentions in GitHub notifications.

Checks for unread notifications where reason == "mention", reads the
thread context, generates a response via Claude (sonnet, max_turns=1),
posts the response, and marks the notification as read.

Max 2 Claude calls per cycle.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from osbot.comms.phrases import contains_banned, scrub_banned
from osbot.config import settings
from osbot.log import get_logger
from osbot.types import (
    ClaudeGatewayProtocol,
    GitHubCLIProtocol,
    Phase,
    Priority,
)

logger = get_logger(__name__)

_MAX_NOTIFICATIONS_PER_CYCLE = 2
_NOTIFY_TIMEOUT_SEC = 60.0


async def _fetch_mentions(github: GitHubCLIProtocol) -> list[dict[str, Any]]:
    """Fetch unread notifications with reason == mention.

    Uses ``gh api notifications`` and filters for mentions.
    """
    result = await github.run_gh([
        "api", "notifications",
        "--method", "GET",
        "-f", "all=false",
    ])
    if not result.success:
        logger.debug("notify_fetch_failed", stderr=result.stderr[:200])
        return []

    try:
        notifications = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(notifications, list):
        return []

    # Filter for mentions only (unread notifications where reason is "mention")
    mentions = [
        n for n in notifications
        if n.get("reason") == "mention" and n.get("unread", False) is True
    ]

    return mentions


async def _get_thread_context(
    github: GitHubCLIProtocol,
    notification: dict[str, Any],
) -> dict[str, Any]:
    """Read the thread context for a notification.

    Fetches the issue/PR body and recent comments to understand the
    conversation context for generating a response.
    """
    subject = notification.get("subject", {})
    subject_url = subject.get("url", "")
    subject_type = subject.get("type", "")
    subject_title = subject.get("title", "")

    repo_data = notification.get("repository", {})
    repo_full_name = repo_data.get("full_name", "")

    context: dict[str, Any] = {
        "repo": repo_full_name,
        "title": subject_title,
        "type": subject_type,
        "body": "",
        "recent_comments": [],
        "thread_id": notification.get("id", ""),
        "subject_url": subject_url,
    }

    if not subject_url:
        return context

    # Fetch the subject (issue or PR) details via the API URL
    # The URL looks like https://api.github.com/repos/owner/name/issues/123
    # We can call it directly via gh api
    result = await github.run_gh(["api", subject_url])
    if result.success:
        try:
            data = json.loads(result.stdout)
            context["body"] = (data.get("body") or "")[:2000]
            context["number"] = data.get("number")
        except (json.JSONDecodeError, TypeError):
            pass

    # Fetch recent comments on the thread
    comments_url = subject.get("latest_comment_url", "")
    if not comments_url and subject_url:
        comments_url = subject_url + "/comments"

    if comments_url and "/comments" in comments_url:
        # Use the parent comments URL (not latest_comment_url which points to one comment)
        if "/comments/" in comments_url:
            # Extract base comments URL from specific comment URL
            comments_url = comments_url.rsplit("/comments/", 1)[0] + "/comments"

        result = await github.run_gh([
            "api", comments_url,
            "--method", "GET",
            "-f", "per_page=10",
            "-f", "direction=desc",
        ])
        if result.success:
            try:
                comments = json.loads(result.stdout)
                bot_login = settings.github_username.lower()
                if isinstance(comments, list):
                    recent = comments[-5:]  # Last 5 comments
                    filtered = [
                        {
                            "author": (c.get("user") or {}).get("login", ""),
                            "body": (c.get("body") or "")[:500],
                        }
                        for c in recent
                        if (c.get("user") or {}).get("login", "").lower() != bot_login
                    ]
                    context["recent_comments"] = filtered
                    # Flag when ALL recent comments are from the bot (self-talk)
                    if recent and not filtered:
                        context["_all_comments_from_self"] = True
            except (json.JSONDecodeError, TypeError):
                pass

    return context


def _build_notify_prompt(context: dict[str, Any]) -> str:
    """Build a prompt for Claude to respond to a mention."""
    repo = context.get("repo", "")
    title = context.get("title", "")
    body = context.get("body", "")[:1000]
    thread_type = context.get("type", "Issue")
    comments = context.get("recent_comments", [])

    comments_text = ""
    if comments:
        comment_lines = []
        for c in comments:
            author = c.get("author", "unknown")
            cbody = c.get("body", "")[:300]
            comment_lines.append(f"  @{author}: {cbody}")
        comments_text = "\nRecent comments:\n" + "\n".join(comment_lines)

    bot_note = f"Your GitHub username is {settings.github_username}. Do not reference or quote your own previous comments.\n" if settings.github_username else ""

    return (
        f"You were mentioned in a GitHub {thread_type}. Write a response.\n"
        f"Repo: {repo}\n"
        f"Title: {title}\n"
        f"Body: {body}\n"
        f"{comments_text}\n\n"
        f"{bot_note}"
        f"Write a brief, relevant response (2-4 sentences). Requirements:\n"
        f"- Address what was asked or mentioned about you specifically\n"
        f"- If asked about a PR you submitted, reference the specific changes\n"
        f"- If asked a technical question, give a concrete answer\n"
        f"- If tagged to review something, provide a substantive observation\n"
        f"- Do NOT use greetings, praise, or closing pleasantries\n"
        f"- Do NOT say 'I\'d be happy to' or 'Great question' or any AI filler\n"
        f"- Write as a developer responding to a colleague\n"
        f"- Output ONLY the response text, no preamble"
    )


async def _post_response(
    github: GitHubCLIProtocol,
    context: dict[str, Any],
    response_text: str,
) -> bool:
    """Post a response comment to the thread.

    Returns True if the comment was posted successfully.
    """
    repo = context.get("repo", "")
    number = context.get("number")
    thread_type = context.get("type", "")

    if not repo or number is None:
        logger.warning("notify_no_target", context_keys=list(context.keys()))
        return False

    if thread_type == "PullRequest":
        result = await github.run_gh([
            "pr", "comment", str(number),
            "--repo", repo,
            "--body", response_text,
        ])
    else:
        # Default to issue comment
        result = await github.run_gh([
            "issue", "comment", str(number),
            "--repo", repo,
            "--body", response_text,
        ])

    return result.success


async def _mark_notification_read(
    github: GitHubCLIProtocol,
    thread_id: str,
) -> None:
    """Mark a notification thread as read."""
    if not thread_id:
        return
    result = await github.run_gh([
        "api", "-X", "PATCH",
        f"notifications/threads/{thread_id}",
    ])
    if not result.success:
        logger.debug("notify_mark_read_failed", thread_id=thread_id)


async def run_notify_phase(
    github: GitHubCLIProtocol,
    gateway: ClaudeGatewayProtocol,
) -> None:
    """Execute the notify phase: check mentions, respond, mark read."""
    logger.info("phase_notify_start")

    try:
        mentions = await _fetch_mentions(github)
    except Exception as exc:
        logger.error("notify_fetch_error", error=str(exc))
        return

    if not mentions:
        logger.debug("phase_notify_done", mentions=0)
        return

    logger.info("notify_mentions_found", count=len(mentions))

    # Process up to MAX_NOTIFICATIONS_PER_CYCLE
    to_process = mentions[:_MAX_NOTIFICATIONS_PER_CYCLE]
    responses_posted = 0

    for notification in to_process:
        thread_id = notification.get("id", "")
        subject_title = notification.get("subject", {}).get("title", "")

        try:
            # Fetch thread context
            context = await _get_thread_context(github, notification)

            if not context.get("repo"):
                # Mark as read even if we can't respond
                await _mark_notification_read(github, thread_id)
                continue

            # Guard: if ALL recent comments in the thread are from the bot,
            # the bot would be responding to itself.  Skip entirely.
            if context.get("_all_comments_from_self"):
                logger.info(
                    "notify_skip_self_talk",
                    repo=context.get("repo"),
                    number=context.get("number"),
                    thread_id=thread_id,
                )
                await _mark_notification_read(github, thread_id)
                continue

            # Generate response via Claude
            prompt = _build_notify_prompt(context)
            result = await gateway.invoke(
                prompt,
                phase=Phase.NOTIFY,
                model="sonnet",
                allowed_tools=[],
                cwd=None,
                timeout=_NOTIFY_TIMEOUT_SEC,
                priority=Priority.FEEDBACK_RESPONSE,
                max_turns=1,
            )

            if not result.success or not result.text.strip():
                logger.warning(
                    "notify_generation_failed",
                    thread_id=thread_id,
                    title=subject_title,
                    error=result.error,
                )
                # Still mark as read to avoid retrying a broken notification
                await _mark_notification_read(github, thread_id)
                continue

            # Scrub banned phrases
            response_text = scrub_banned(result.text.strip())

            if len(response_text) < 20:
                logger.info("notify_too_short_after_scrub", thread_id=thread_id)
                await _mark_notification_read(github, thread_id)
                continue

            # Double-check no banned phrases remain
            remaining = contains_banned(response_text)
            if remaining:
                logger.warning(
                    "notify_banned_phrases_after_scrub",
                    thread_id=thread_id,
                    phrases=remaining[:3],
                )
                await _mark_notification_read(github, thread_id)
                continue

            # Post the response
            posted = await _post_response(github, context, response_text)

            if posted:
                responses_posted += 1
                logger.info(
                    "notify_response_posted",
                    repo=context.get("repo"),
                    number=context.get("number"),
                    response_len=len(response_text),
                )
            else:
                logger.warning(
                    "notify_post_failed",
                    repo=context.get("repo"),
                    number=context.get("number"),
                )

            # Mark as read regardless of whether we posted
            await _mark_notification_read(github, thread_id)

        except Exception as exc:
            logger.error(
                "notify_error",
                thread_id=thread_id,
                title=subject_title,
                error=str(exc),
                exc_info=True,
            )
            # Try to mark as read to avoid retry loops
            with contextlib.suppress(Exception):
                await _mark_notification_read(github, thread_id)

    logger.info("phase_notify_done", responses=responses_posted, mentions_total=len(mentions))
