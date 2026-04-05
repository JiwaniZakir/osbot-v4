"""Repo signal collector -- compute external signals for scoring.

For each candidate repo, fetches: external merge rate, average response
hours, close-completion rate, and CI presence.  Results are cached in
``memory.db.repo_signals`` with a 7-day TTL.

Zero Claude calls.  Layer 4.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from osbot.intel.graphql import GraphQLClient
from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import GitHubCLIProtocol, MemoryDBProtocol, RepoMeta

logger = get_logger(__name__)

# Cache TTL for repo signals.
_CACHE_TTL_DAYS = 7


async def compute_signals(
    repo: RepoMeta,
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
) -> dict[str, Any]:
    """Compute external signals for a repo and cache them.

    Signals computed:
    - ``external_merge_rate``: merged PRs / total closed PRs in recent
      history, filtering to non-collaborator authors only.
    - ``avg_response_hours``: mean time to first maintainer comment on PRs.
    - ``close_completion_rate``: fraction of closed PRs that were merged
      (vs. closed without merge).
    - ``has_ci``: whether GitHub Actions workflows exist.

    Args:
        repo: Repository metadata.
        github: CLI protocol for GraphQL and ``gh`` commands.
        db: Memory database for caching.

    Returns:
        Dict with signal values.
    """
    full_name = repo.full_name

    # Check cache
    cached = await db.fetchone(
        "SELECT * FROM repo_signals WHERE repo = ? AND expires_at > datetime('now')",
        (full_name,),
    )
    if cached is not None:
        logger.debug("signals_cached", repo=full_name)
        return dict(cached)

    graphql = GraphQLClient(github)
    owner, name = repo.owner, repo.name

    # Fetch recent PRs via GraphQL
    health = await graphql.repo_health(owner, name)
    recent_prs: list[dict[str, Any]] = health.get("recent_prs", [])
    has_ci: bool = health.get("has_ci", False)

    # Compute external merge rate (non-collaborator PRs only)
    ext_merged = 0
    ext_total = 0
    response_hours_list: list[float] = []

    for pr in recent_prs:
        assoc = (pr.get("authorAssociation") or "").upper()
        # Non-collaborator: not MEMBER, OWNER, or COLLABORATOR
        is_external = assoc not in ("MEMBER", "OWNER", "COLLABORATOR")

        if is_external:
            ext_total += 1
            if pr.get("state") == "MERGED" or pr.get("mergedAt"):
                ext_merged += 1

        # Compute response time from created -> merged/closed for all PRs
        created_str = pr.get("createdAt", "")
        closed_str = pr.get("closedAt") or pr.get("mergedAt") or ""
        if created_str and closed_str:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                closed_dt = datetime.fromisoformat(closed_str.replace("Z", "+00:00"))
                hours = (closed_dt - created_dt).total_seconds() / 3600.0
                if 0 < hours < 720:  # Cap at 30 days to exclude stale PRs
                    response_hours_list.append(hours)
            except (ValueError, TypeError):
                pass

    external_merge_rate = ext_merged / ext_total if ext_total > 0 else 0.0
    avg_response_hours = (
        sum(response_hours_list) / len(response_hours_list)
        if response_hours_list
        else 0.0
    )

    # Close-completion rate (all PRs)
    total_closed = len(recent_prs)
    total_merged = sum(
        1 for pr in recent_prs if pr.get("state") == "MERGED" or pr.get("mergedAt")
    )
    close_completion_rate = total_merged / total_closed if total_closed > 0 else 0.0

    signals: dict[str, Any] = {
        "repo": full_name,
        "external_merge_rate": round(external_merge_rate, 4),
        "avg_response_hours": round(avg_response_hours, 2),
        "close_completion_rate": round(close_completion_rate, 4),
        "has_ci": has_ci,
        "ext_pr_count": ext_total,
    }

    # Cache in DB
    await _cache_signals(full_name, signals, db)

    logger.info("signals_computed", **signals)
    return signals


async def _cache_signals(
    repo: str, signals: dict[str, Any], db: MemoryDBProtocol
) -> None:
    """Insert or replace repo signals in the cache table."""
    await db.execute(
        """
        INSERT OR REPLACE INTO repo_signals
            (repo, external_merge_rate, avg_response_hours,
             close_completion_rate, ci_enabled,
             last_computed, expires_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'),
                datetime('now', '+' || ? || ' days'))
        """,
        (
            repo,
            signals["external_merge_rate"],
            signals["avg_response_hours"],
            signals["close_completion_rate"],
            1 if signals.get("has_ci", False) else 0,
            str(_CACHE_TTL_DAYS),
        ),
    )
