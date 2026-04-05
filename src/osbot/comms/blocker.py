"""Blocker notification — email the owner when the bot hits a wall.

Instead of crashing or silently skipping, the bot emails the owner
with a description of the blocker and what action is needed.
The bot then continues processing other work.

Blocker types:
  - auth_expired: OAuth token expired, needs re-login
  - cla_required: CLA needs signing on a PR
  - ci_failed: CI failed on our PR, may need manual fix
  - maintainer_question: Maintainer asked a question we can't answer
  - assignment_needed: Repo requires manual assignment/approval
  - health_failed: Critical health check failed
  - screenshot_requested: Maintainer asked for screenshots/visual proof
  - manual_action: Any other action requiring human intervention

Each blocker is logged and deduplicated (same blocker not emailed twice
within 24 hours).
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from osbot.config import settings
from osbot.log import get_logger

logger = get_logger(__name__)

# Dedup tracker: {blocker_key: last_notified_iso}
_DEDUP_FILE = Path(settings.state_dir) / "blocker_dedup.json"
_DEDUP_WINDOW_HOURS = 24


def _load_dedup() -> dict[str, str]:
    try:
        if _DEDUP_FILE.exists():
            return json.loads(_DEDUP_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_dedup(data: dict[str, str]) -> None:
    with contextlib.suppress(OSError):
        _DEDUP_FILE.write_text(json.dumps(data, indent=2))


def _should_notify(key: str) -> bool:
    """Return True if this blocker hasn't been notified recently."""
    dedup = _load_dedup()
    last = dedup.get(key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if datetime.now(UTC) - last_dt > timedelta(hours=_DEDUP_WINDOW_HOURS):
            return True
    except (ValueError, TypeError):
        return True
    return False


def _mark_notified(key: str) -> None:
    dedup = _load_dedup()
    dedup[key] = datetime.now(UTC).isoformat()
    # Prune old entries
    cutoff = (datetime.now(UTC) - timedelta(hours=_DEDUP_WINDOW_HOURS * 2)).isoformat()
    dedup = {k: v for k, v in dedup.items() if v > cutoff}
    _save_dedup(dedup)


# ---------------------------------------------------------------------------
# Blocker templates
# ---------------------------------------------------------------------------

_TEMPLATES: dict[str, dict[str, str]] = {
    "auth_expired": {
        "subject": "[osbot] ACTION REQUIRED: OAuth token expired",
        "body": (
            "The Claude OAuth token has expired. The bot cannot make any Claude calls.\n\n"
            "To fix:\n"
            "1. ssh aegis-ext\n"
            "2. docker exec -it osbot-v4 claude auth login\n"
            "3. Follow the browser URL to re-authenticate\n\n"
            "The bot will continue running discovery and monitoring but cannot "
            "implement fixes or respond to feedback until re-authenticated."
        ),
    },
    "cla_required": {
        "subject": "[osbot] CLA signing needed: {repo}#{pr_number}",
        "body": (
            "A PR was submitted to a repo that requires CLA signing.\n\n"
            "Repository: {repo}\n"
            "PR: {pr_url}\n\n"
            "Please sign the CLA at the link provided by the CLA bot comment "
            "on the PR. The PR will remain open until signed.\n\n"
            "The bot will NOT close this PR — it's waiting for you."
        ),
    },
    "ci_failed": {
        "subject": "[osbot] CI failed on {repo}#{pr_number}",
        "body": (
            "Our PR has failing CI checks that the bot couldn't fix automatically.\n\n"
            "Repository: {repo}\n"
            "PR: {pr_url}\n\n"
            "You may need to manually investigate the CI failure.\n"
            "The bot has attempted one fix but the issue persists."
        ),
    },
    "maintainer_question": {
        "subject": "[osbot] Maintainer asked a question on {repo}#{pr_number}",
        "body": (
            "A maintainer asked a question that the bot cannot confidently answer.\n\n"
            "Repository: {repo}\n"
            "PR: {pr_url}\n"
            "Question: {question}\n\n"
            "Please respond manually or provide guidance."
        ),
    },
    "screenshot_requested": {
        "subject": "[osbot] Screenshots requested on {repo}#{pr_number}",
        "body": (
            "A maintainer requested screenshots or visual proof for our PR.\n\n"
            "Repository: {repo}\n"
            "PR: {pr_url}\n\n"
            "The bot cannot take screenshots. Please provide them manually,\n"
            "or reply to the maintainer explaining the fix was verified via tests."
        ),
    },
    "assignment_needed": {
        "subject": "[osbot] Assignment approval needed for {repo}#{issue_number}",
        "body": (
            "A high-value issue requires assignment that hasn't been granted.\n\n"
            "Repository: {repo}\n"
            "Issue: {issue_url}\n\n"
            "The bot posted a claim comment but hasn't been assigned.\n"
            "You may want to follow up manually."
        ),
    },
    "health_failed": {
        "subject": "[osbot] CRITICAL: Health check failed — bot halted",
        "body": (
            "The bot's health check failed and it has halted.\n\n"
            "Failed checks: {failed_checks}\n\n"
            "The bot will keep retrying on restart (Docker auto-restart),\n"
            "but the underlying issue needs manual resolution.\n\n"
            "Common fixes:\n"
            "- auth_expired: docker exec -it osbot-v4 claude auth login\n"
            "- github: check GH_TOKEN in .env\n"
            "- memory_db: check disk space on VPS"
        ),
    },
    "generic": {
        "subject": "[osbot] Action required: {reason}",
        "body": (
            "The bot hit a blocker and needs your help.\n\n"
            "Type: {blocker_type}\n"
            "Details: {details}\n\n"
            "The bot is continuing to process other work, but this "
            "specific item is paused until resolved."
        ),
    },
}


async def notify_blocker(
    blocker_type: str,
    **kwargs: str,
) -> bool:
    """Email the owner about a blocker and log it.

    The bot does NOT crash or skip — it notifies and continues.
    Deduplicates: same blocker is not emailed twice within 24 hours.

    Args:
        blocker_type: One of the template keys (auth_expired, cla_required, etc.)
        **kwargs: Template variables (repo, pr_number, pr_url, question, etc.)

    Returns:
        True if notification was sent or deduplicated. False on send failure.
    """
    # Build dedup key from type + identifying info
    key_parts = [blocker_type]
    for k in ("repo", "pr_number", "issue_number"):
        if k in kwargs:
            key_parts.append(str(kwargs[k]))
    dedup_key = ":".join(key_parts)

    if not _should_notify(dedup_key):
        logger.debug("blocker_deduped", type=blocker_type, key=dedup_key)
        return True

    # Get template
    template = _TEMPLATES.get(blocker_type, _TEMPLATES["generic"])
    subject = template["subject"]
    body = template["body"]

    # Fill in kwargs
    kwargs.setdefault("blocker_type", blocker_type)
    kwargs.setdefault("reason", blocker_type.replace("_", " "))
    kwargs.setdefault("details", "No additional details available.")
    for k, v in kwargs.items():
        subject = subject.replace(f"{{{k}}}", str(v))
        body = body.replace(f"{{{k}}}", str(v))

    # Send
    from osbot.comms.email import send_email

    severity = "critical" if blocker_type in ("auth_expired", "health_failed") else "warning"
    sent = await send_email(
        to=settings.alert_email,
        subject=subject,
        body=body,
        severity=severity,
    )

    if sent:
        _mark_notified(dedup_key)
        logger.info("blocker_notified", type=blocker_type, key=dedup_key)
    else:
        logger.warning("blocker_notification_failed", type=blocker_type)

    return sent
