"""Anti-spam checks -- blacklist only.

No artificial limits. The token management system is the only throughput gate.
The org daily limit (ORG_DAILY_LIMIT=2) caused 27% of v4's early failures
by blocking high-value orgs like pytorch, huggingface, modelscope.

The only check remaining is the org blacklist (orgs that actively hunt bot PRs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from osbot.config import settings
from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import MemoryDBProtocol

logger = get_logger(__name__)

# Orgs known to actively hunt and ban bot PRs.
# Add entries here only with strong evidence (public statements, bot bans).
_BLACKLISTED_ORGS: frozenset[str] = frozenset({
    # scikit-learn explicitly bans automated contributions
    "scikit-learn",
})


async def check_spam(
    repo: str,
    db: MemoryDBProtocol,
) -> tuple[bool, str]:
    """Return ``(True, "")`` if the repo passes anti-spam checks.

    Returns ``(False, reason)`` if the repo's org is blacklisted or is
    owned by the bot's own GitHub account.

    No artificial limits — no org daily caps, no repo weekly caps.
    The token balancer and self-diagnostics circuit breakers are
    the only throughput controls.
    """
    org = repo.split("/")[0] if "/" in repo else ""

    # Never submit PRs to the bot's own repos (avoids self-contributions)
    if org.lower() == settings.github_username.lower():
        logger.info("spam_blocked", repo=repo, reason="own_repo", org=org)
        return False, f"repo {repo} is owned by the bot account"

    if org.lower() in _BLACKLISTED_ORGS:
        logger.info("spam_blocked", repo=repo, reason="blacklisted_org", org=org)
        return False, f"org {org} is blacklisted"

    return True, ""
