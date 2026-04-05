"""L1: OAuth usage endpoint poller + token expiry monitoring.

Reads the Claude CLI OAuth token from ``~/.claude`` credentials and
polls ``/api/oauth/usage`` to get current utilization across all
four windows (5-hour, 7-day, Opus weekly, Sonnet weekly).

Also provides ``check_token_expiry()`` to send email alerts when
the OAuth token is about to expire or has already expired.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from osbot.log import get_logger
from osbot.types import UsageSnapshot

logger = get_logger("tokens.probe")

_API_BASE = "https://api.anthropic.com"
_USAGE_PATH = "/api/oauth/usage"
_CRED_PATHS = [
    Path.home() / ".claude" / "credentials.json",
    Path.home() / ".claude" / ".credentials.json",
    Path.home() / ".claude.json",
]

# Common keys the token might be stored under
_TOKEN_KEYS = [
    "oauth_token", "oauthToken", "access_token", "accessToken",
    "token", "bearerToken", "bearer_token",
]

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazily create a shared httpx client with a reasonable timeout."""
    global _client  # noqa: PLW0603
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
    return _client


def _read_oauth_token() -> str | None:
    """Extract the OAuth token from the Claude CLI credentials file.

    Searches multiple possible credential file locations and key names.
    The Claude CLI stores credentials differently depending on version
    and platform (file-based on Linux, keychain on macOS).
    """
    # Try credential files
    for path in _CRED_PATHS:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                # Try all known key names
                for key in _TOKEN_KEYS:
                    token = data.get(key)
                    if token and isinstance(token, str) and len(token) > 20:
                        logger.info("oauth_token_found", source=str(path), key=key)
                        return token
                # Also check nested structures (Claude CLI uses "claudeAiOauth")
                for nested_key in ("claudeAiOauth", "auth", "credentials", "oauth"):
                    nested = data.get(nested_key)
                    if isinstance(nested, dict):
                        for key in _TOKEN_KEYS:
                            token = nested.get(key)
                            if token and isinstance(token, str) and len(token) > 20:
                                logger.info("oauth_token_found", source=f"{path}:{nested_key}", key=key)
                                return token
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("credentials_read_failed", path=str(path), error=str(exc))

    # Try scanning all JSON files in ~/.claude/ for any token-like value
    claude_dir = Path.home() / ".claude"
    if claude_dir.is_dir():
        for json_file in claude_dir.glob("*.json"):
            if json_file.name == "settings.json":
                continue
            try:
                data = json.loads(json_file.read_text())
                if isinstance(data, dict):
                    for key in _TOKEN_KEYS:
                        token = data.get(key)
                        if token and isinstance(token, str) and len(token) > 20:
                            logger.info("oauth_token_found", source=str(json_file), key=key)
                            return token
            except (json.JSONDecodeError, OSError):
                continue

    logger.debug("oauth_token_not_found", searched=str(_CRED_PATHS))
    return None


async def probe(oauth_token: str | None = None) -> UsageSnapshot | None:
    """Poll the OAuth usage endpoint and return a snapshot.

    Args:
        oauth_token: Bearer token.  If ``None``, reads from ``~/.claude``.

    Returns:
        A ``UsageSnapshot`` with current utilization, or ``None`` on failure.
    """
    token = oauth_token or _read_oauth_token()
    if not token:
        logger.warning("probe_no_token", msg="No OAuth token available")
        return None

    client = _get_client()
    try:
        resp = await client.get(
            f"{_API_BASE}{_USAGE_PATH}",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code == 401:
            logger.warning("probe_auth_error", status=401)
            return None
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("probe_http_error", status=exc.response.status_code)
        return None
    except httpx.RequestError as exc:
        logger.warning("probe_network_error", error=str(exc))
        return None

    # Parse the response — structure: {five_hour, seven_day, opus_weekly, sonnet_weekly}
    # Each value is 0.0-1.0 utilization fraction.
    try:
        return UsageSnapshot(
            ts=datetime.now(timezone.utc).isoformat(),
            five_hour=float(body.get("five_hour", body.get("fiveHour", 0.0))),
            seven_day=float(body.get("seven_day", body.get("sevenDay", 0.0))),
            opus_weekly=float(body.get("opus_weekly", body.get("opusWeekly", 0.0))),
            sonnet_weekly=float(body.get("sonnet_weekly", body.get("sonnetWeekly", 0.0))),
        )
    except (TypeError, ValueError) as exc:
        logger.warning("probe_parse_error", error=str(exc), body=str(body)[:200])
        return None


# ---------------------------------------------------------------------------
# Expiry keys the token expiration might be stored under
# ---------------------------------------------------------------------------
_EXPIRY_KEYS = [
    "expiresAt", "expires_at", "expiry", "exp",
    "token_expiry", "tokenExpiry", "expires",
]


def _read_token_expiry() -> datetime | None:
    """Extract the OAuth token expiration datetime from credentials.

    Searches multiple credential file locations and key names.
    Returns a timezone-aware datetime, or None if no expiry found.
    """
    for path in _CRED_PATHS:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                continue

            # Check top-level keys
            expiry = _extract_expiry_from_dict(data)
            if expiry is not None:
                return expiry

            # Check nested structures (Claude CLI uses "claudeAiOauth")
            for nested_key in ("claudeAiOauth", "auth", "credentials", "oauth"):
                nested = data.get(nested_key)
                if isinstance(nested, dict):
                    expiry = _extract_expiry_from_dict(nested)
                    if expiry is not None:
                        return expiry

        except (json.JSONDecodeError, OSError):
            continue

    # Scan all JSON files in ~/.claude/
    claude_dir = Path.home() / ".claude"
    if claude_dir.is_dir():
        for json_file in claude_dir.glob("*.json"):
            if json_file.name == "settings.json":
                continue
            try:
                data = json.loads(json_file.read_text())
                if isinstance(data, dict):
                    expiry = _extract_expiry_from_dict(data)
                    if expiry is not None:
                        return expiry
            except (json.JSONDecodeError, OSError):
                continue

    return None


def _extract_expiry_from_dict(data: dict[str, object]) -> datetime | None:
    """Try to parse an expiry value from a dict using known key names."""
    for key in _EXPIRY_KEYS:
        val = data.get(key)
        if val is None:
            continue

        # Handle epoch timestamps (seconds or milliseconds)
        if isinstance(val, (int, float)) and val > 1_000_000_000:
            try:
                # Detect milliseconds (13+ digits) vs seconds (10 digits)
                ts = val / 1000 if val > 1_000_000_000_000 else val
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (OSError, ValueError):
                continue

        # Handle ISO 8601 string
        if isinstance(val, str) and len(val) > 8:
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, TypeError):
                continue

    return None


# Track whether we already sent an alert to avoid spam within the same run
_expiry_alert_sent: dict[str, bool] = {}


async def check_token_expiry(alert_email: str | None = None) -> None:
    """Check OAuth token expiry and send email alerts if needed.

    Sends an alert if:
      - Token expires within 24 hours (warning)
      - Token is already expired (critical)

    Deduplicates alerts: only sends once per severity level per process
    lifetime (resets on restart, which is fine for Docker).

    Args:
        alert_email: Override recipient. Falls back to ``settings.alert_email``.
    """
    from osbot.config import settings as cfg

    email = alert_email or cfg.alert_email
    if not email:
        logger.debug("token_expiry_skip", reason="no alert_email configured")
        return

    expiry = _read_token_expiry()
    if expiry is None:
        # No expiry info found -- common for some auth methods.
        # Try probing the API to see if the token is still valid.
        token = _read_oauth_token()
        if token is None:
            # No token at all -- health check will catch this
            return

        # Attempt a probe to see if the token works
        client = _get_client()
        try:
            resp = await client.get(
                f"{_API_BASE}{_USAGE_PATH}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401:
                # Token is expired/invalid
                if _expiry_alert_sent.get("expired"):
                    return
                _expiry_alert_sent["expired"] = True

                from osbot.comms.email import send_email
                await send_email(
                    to=email,
                    subject="[osbot] CRITICAL: OAuth token is expired or invalid",
                    body=(
                        "Your Claude OAuth token is expired or invalid (got HTTP 401).\n"
                        "The bot cannot make any Claude calls until re-authenticated.\n\n"
                        "To re-authenticate:\n\n"
                        "1. SSH into the server: ssh aegis-ext\n"
                        "2. Run: docker exec -it osbot-v4 claude auth login\n"
                        "3. Follow the browser URL to re-authenticate\n\n"
                        "The bot will stop working until this is resolved."
                    ),
                    severity="critical",
                )
                logger.error("token_expired_alert_sent", to=email)
        except httpx.RequestError:
            # Network error -- not a token issue
            pass
        return

    now = datetime.now(timezone.utc)
    remaining = expiry - now
    hours_remaining = remaining.total_seconds() / 3600

    if remaining.total_seconds() <= 0:
        # Already expired
        if _expiry_alert_sent.get("expired"):
            return
        _expiry_alert_sent["expired"] = True

        from osbot.comms.email import send_email
        await send_email(
            to=email,
            subject="[osbot] CRITICAL: OAuth token has EXPIRED",
            body=(
                f"Your Claude OAuth token expired at {expiry.isoformat()}.\n"
                f"The bot cannot make any Claude calls until re-authenticated.\n\n"
                f"To re-authenticate:\n\n"
                f"1. SSH into the server: ssh aegis-ext\n"
                f"2. Run: docker exec -it osbot-v4 claude auth login\n"
                f"3. Follow the browser URL to re-authenticate\n\n"
                f"The bot will stop working until this is resolved."
            ),
            severity="critical",
        )
        logger.error("token_expired_alert_sent", to=email, expired_at=expiry.isoformat())

    elif hours_remaining <= 24:
        # Expiring soon
        if _expiry_alert_sent.get("expiring_soon"):
            return
        _expiry_alert_sent["expiring_soon"] = True

        hours_int = int(hours_remaining)
        from osbot.comms.email import send_email
        await send_email(
            to=email,
            subject=f"[osbot] OAuth token expires in {hours_int}h -- re-authenticate required",
            body=(
                f"Your Claude OAuth token expires at {expiry.isoformat()}.\n"
                f"That is approximately {hours_int} hours from now.\n\n"
                f"To re-authenticate:\n\n"
                f"1. SSH into the server: ssh aegis-ext\n"
                f"2. Run: docker exec -it osbot-v4 claude auth login\n"
                f"3. Follow the browser URL to re-authenticate\n\n"
                f"The bot will stop working when the token expires."
            ),
            severity="warning",
        )
        logger.warning(
            "token_expiring_soon_alert_sent",
            to=email,
            expires_at=expiry.isoformat(),
            hours_remaining=hours_int,
        )
    else:
        logger.debug(
            "token_expiry_ok",
            expires_at=expiry.isoformat(),
            hours_remaining=round(hours_remaining, 1),
        )
