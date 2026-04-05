"""Email notifications -- send alerts via webhook-to-email service.

Supports three delivery methods, tried in order:
  1. email_webhook_url -- POST JSON to a webhook that forwards to email
     (e.g., Zapier, IFTTT, Make, n8n, Pipedream)
  2. Existing Discord/Slack webhook -- falls back to send_alert() with
     an email-style prefix so the user sees it in their chat channel
  3. Silent skip -- if nothing is configured, logs and returns True

The webhook payload format:
  {"to": "...", "subject": "...", "body": "...", "severity": "..."}

Any webhook-to-email bridge can consume this.  For Zapier: create a
"Webhooks by Zapier" trigger -> Gmail/Outlook send-email action.
"""

from __future__ import annotations

import httpx

from osbot.config import settings
from osbot.log import get_logger

logger = get_logger(__name__)

_TIMEOUT = 15.0  # seconds


async def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    severity: str = "info",
) -> bool:
    """Send an email notification.

    Tries the email webhook first, then falls back to the chat webhook.

    Args:
        to: Recipient email address (informational -- the webhook may
            override this with its own configured recipient).
        subject: Email subject line.
        body: Plain-text body.
        severity: One of ``"info"``, ``"warning"``, ``"high"``, ``"critical"``.

    Returns:
        True if the notification was sent (or silently skipped).
        False if the send failed.
    """
    # Strategy 1: dedicated email webhook
    email_url = settings.email_webhook_url
    if email_url:
        return await _send_via_email_webhook(email_url, to, subject, body, severity)

    # Strategy 2: fall back to chat webhook (Discord/Slack) with email context
    webhook_url = settings.webhook_url
    if webhook_url:
        return await _send_via_chat_webhook(to, subject, body, severity)

    # Strategy 3: nothing configured -- log and skip
    logger.info(
        "email_skipped",
        reason="no email_webhook_url or webhook_url configured",
        subject=subject,
        to=to,
    )
    return True


async def _send_via_email_webhook(
    url: str,
    to: str,
    subject: str,
    body: str,
    severity: str,
) -> bool:
    """POST to a webhook-to-email bridge."""
    payload = {
        "to": to,
        "subject": subject,
        "body": body,
        "severity": severity,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "email_webhook_failed",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                return False
            logger.info("email_sent_via_webhook", subject=subject, to=to)
            return True
    except Exception as exc:
        logger.warning("email_webhook_error", error=str(exc), subject=subject)
        return False


async def _send_via_chat_webhook(
    to: str,
    subject: str,
    body: str,
    severity: str,
) -> bool:
    """Fall back to the existing chat webhook with email-style formatting."""
    from osbot.comms.webhook import send_alert

    # Format the message so it stands out in the chat channel
    message = f"EMAIL ALERT for {to}\nSubject: {subject}\n---\n{body}"
    return await send_alert(message, severity=severity)
