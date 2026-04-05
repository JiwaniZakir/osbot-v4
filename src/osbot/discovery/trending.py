"""Trending repo discovery -- find repos with recent star velocity.

Searches for recently created repos with high star counts (velocity proxy)
and repos with recent issue spikes (active development).  Applies the same
domain and ban filters as ``repo_finder``.

Called from the learn phase (every 12h), not every discovery cycle.
Results are inserted into ``repo_signals`` so the next discovery cycle
picks them up automatically.

Zero Claude calls.  Layer 4.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from osbot.config import settings
from osbot.log import get_logger
from osbot.safety.domain import is_in_domain
from osbot.types import GitHubCLIProtocol, MemoryDBProtocol, RepoMeta

logger = get_logger(__name__)

# How far back to look for "recently created" repos.
_CREATED_WINDOW_DAYS = 30

# Minimum stars to count as high-velocity for a young repo.
_MIN_STARS_TRENDING = 100

# How many results per search batch.
_SEARCH_LIMIT = 30


async def find_trending_repos(
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
) -> list[RepoMeta]:
    """Search GitHub for trending repos and return candidates.

    Two search strategies:
    1. **Star velocity** -- repos created in the last 30 days with >100 stars,
       sorted by stars descending.  A repo that hits 100+ stars in its first
       month has strong community interest.
    2. **Issue spike** -- repos with recent pushes sorted by number of
       help-wanted issues, indicating active development with fresh issues.

    Applies ``is_in_domain`` filtering and ban checks, identical to
    ``repo_finder``.  Deduplicates against repos already cached in
    ``repo_signals`` with a valid (non-expired) entry.

    Args:
        github: CLI protocol for ``gh search repos``.
        db: Memory database for cache and ban checks.

    Returns:
        List of ``RepoMeta`` candidates for signal enrichment.
    """
    candidates: list[RepoMeta] = []
    seen: set[str] = set()

    # -- Strategy 1: Star velocity (young repos with high stars) --
    star_repos = await _search_star_velocity(github)
    for repo_data in star_repos:
        meta = _collect_candidate(repo_data, seen)
        if meta is not None:
            candidates.append(meta)

    # -- Strategy 2: Issue spike (active repos with fresh issues) --
    issue_repos = await _search_issue_spike(github)
    for repo_data in issue_repos:
        meta = _collect_candidate(repo_data, seen)
        if meta is not None:
            candidates.append(meta)

    # -- Filter: domain, stars, bans, cache --
    filtered: list[RepoMeta] = []
    for meta in candidates:
        if not is_in_domain(meta):
            continue

        if meta.stars < settings.repo_min_stars or meta.stars > settings.repo_max_stars:
            continue

        if await _has_valid_cache(meta.full_name, db):
            continue

        if await db.is_repo_banned(meta.full_name):
            logger.debug("trending_banned_skip", repo=meta.full_name)
            continue

        filtered.append(meta)

    logger.info(
        "trending_repos_found",
        raw=len(candidates),
        filtered=len(filtered),
        seen=len(seen),
    )
    return filtered


async def _search_star_velocity(
    github: GitHubCLIProtocol,
) -> list[dict[str, Any]]:
    """Search for repos created recently with high star counts."""
    cutoff = datetime.now(UTC) - timedelta(days=_CREATED_WINDOW_DAYS)
    created_qualifier = f"created:>={cutoff.strftime('%Y-%m-%d')}"

    all_results: list[dict[str, Any]] = []

    for language in settings.allowed_languages:
        result = await github.run_gh([
            "search", "repos",
            "--language", language,
            "--sort", "stars",
            "--order", "desc",
            "--limit", str(_SEARCH_LIMIT),
            "--json", "fullName,description,language,stargazersCount,pushedAt,isArchived,repositoryTopics",
            "--",
            f"stars:>={_MIN_STARS_TRENDING}",
            created_qualifier,
        ])

        if not result.success:
            logger.debug(
                "trending_star_search_failed",
                language=language,
                stderr=result.stderr[:200],
            )
            continue

        try:
            data: list[dict[str, Any]] = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            continue

        all_results.extend(r for r in data if not r.get("isArchived", False))

    return all_results


async def _search_issue_spike(
    github: GitHubCLIProtocol,
) -> list[dict[str, Any]]:
    """Search for repos with recent push activity and help-wanted issues."""
    cutoff = datetime.now(UTC) - timedelta(days=settings.repo_max_push_age_days)
    pushed_qualifier = f"pushed:>={cutoff.strftime('%Y-%m-%d')}"

    all_results: list[dict[str, Any]] = []

    for language in settings.allowed_languages:
        result = await github.run_gh([
            "search", "repos",
            "--language", language,
            "--sort", "updated",
            "--order", "desc",
            "--limit", str(_SEARCH_LIMIT),
            "--json", "fullName,description,language,stargazersCount,pushedAt,isArchived,repositoryTopics",
            "--",
            "help-wanted-issues:>5",
            f"stars:{settings.repo_min_stars}..{settings.repo_max_stars}",
            pushed_qualifier,
        ])

        if not result.success:
            logger.debug(
                "trending_issue_search_failed",
                language=language,
                stderr=result.stderr[:200],
            )
            continue

        try:
            data: list[dict[str, Any]] = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            continue

        all_results.extend(r for r in data if not r.get("isArchived", False))

    return all_results


def _collect_candidate(
    data: dict[str, Any],
    seen: set[str],
) -> RepoMeta | None:
    """Convert a search result to RepoMeta, deduplicating via *seen*.

    Returns ``None`` if the repo was already seen or cannot be parsed.
    """
    full_name = data.get("fullName", "")
    if not full_name or full_name in seen:
        return None
    seen.add(full_name)

    if "/" not in full_name:
        return None

    owner, name = full_name.split("/", 1)
    language = data.get("language") or ""
    stars = data.get("stargazersCount", 0)
    description = data.get("description") or ""

    # Extract topics from repositoryTopics if available
    raw_topics = data.get("repositoryTopics", [])
    topics: list[str] = []
    if isinstance(raw_topics, list):
        for entry in raw_topics:
            if isinstance(entry, dict):
                topic_name = entry.get("name", "")
                if topic_name:
                    topics.append(topic_name)
            elif isinstance(entry, str):
                topics.append(entry)

    return RepoMeta(
        owner=owner,
        name=name,
        language=language,
        stars=stars,
        description=description,
        topics=topics,
        last_push_at=data.get("pushedAt") or "",
    )


async def _has_valid_cache(repo: str, db: MemoryDBProtocol) -> bool:
    """Return True if the repo has a non-expired entry in repo_signals."""
    row = await db.fetchone(
        "SELECT 1 FROM repo_signals WHERE repo = ? AND expires_at > datetime('now')",
        (repo,),
    )
    return row is not None
