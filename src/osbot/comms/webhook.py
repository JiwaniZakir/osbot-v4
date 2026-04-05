"""Webhook notifications -- send alerts to Discord or Slack.

Auto-detects format from the URL:
  - discord.com or discordapp.com -> Discord format: {"content": "..."}
  - hooks.slack.com -> Slack format: {"text": "..."}
  - Anything else -> Discord format (most common for generic webhooks)

If webhook_url is empty, all calls silently succeed (no-op).
"""

from __future__ import annotations

import httpx

from osbot.config import settings
from osbot.log import get_logger

logger = get_logger(__name__)

_TIMEOUT = 10.0  # seconds


def _is_slack(url: str) -> bool:
    return "hooks.slack.com" in url


def _format_message(message: str, severity: str) -> str:
    return f"[osbot] [{severity}] {message}"


async def send_alert(message: str, severity: str = "info") -> bool:
    """Send an alert to the configured webhook (Discord/Slack).

    Args:
        message: The alert text.
        severity: One of "info", "warning", "high", "critical".

    Returns:
        True if the alert was sent (or skipped because no URL is configured).
        False if the send failed.
    """
    url = settings.webhook_url
    if not url:
        return True  # No webhook configured -- silently skip

    formatted = _format_message(message, severity)

    if _is_slack(url):
        payload = {"text": formatted}
    else:
        # Discord format (also works for most generic webhooks)
        payload = {"content": formatted}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "webhook_send_failed",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                return False
            logger.debug("webhook_sent", severity=severity, status=resp.status_code)
            return True
    except Exception as exc:
        logger.warning("webhook_send_error", error=str(exc))
        return False
