"""Repo finder -- search GitHub for candidate repositories.

Runs ``gh search repos`` with language, topic, star, and recency filters.
Applies domain filtering via ``osbot.safety.domain.is_in_domain``.
Returns candidate repos not already in the active pool.

Zero Claude calls.  Layer 4.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from osbot.config import settings
from osbot.log import get_logger
from osbot.safety.domain import is_in_domain
from osbot.types import GitHubCLIProtocol, MemoryDBProtocol, RepoMeta

logger = get_logger(__name__)


async def find_repos(
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
) -> list[RepoMeta]:
    """Search GitHub for repos matching language, topic, stars, and recency.

    Deduplicates against repos already scored in ``repo_signals`` with a
    valid (non-expired) cache entry.  Applies ``is_in_domain`` filtering.

    Args:
        github: CLI protocol for ``gh search repos``.
        db: Memory database for checking already-known repos.

    Returns:
        List of new ``RepoMeta`` candidates ready for signal enrichment.
    """
    candidates: list[RepoMeta] = []
    seen: set[str] = set()

    cutoff = datetime.now(UTC) - timedelta(days=settings.repo_max_push_age_days)
    pushed_qualifier = f"pushed:>={cutoff.strftime('%Y-%m-%d')}"

    for language in settings.allowed_languages:
        for keyword in settings.domain_keywords:
            # 2.5s delay between searches to stay under GitHub's 30/min Search API limit
            # 32 combos × 2.5s = 80s total, well within a single discovery cycle
            await asyncio.sleep(2.5)
            repos = await _search_batch(
                github, language, keyword, pushed_qualifier
            )
            for repo_data in repos:
                full_name = repo_data.get("fullName", "")
                if not full_name or full_name in seen:
                    continue
                seen.add(full_name)

                meta = _to_repo_meta(repo_data, keyword=keyword)
                if meta is None:
                    continue

                # Domain filter — the topic: search already ensures domain match,
                # but verify language is in our allowed set
                if not is_in_domain(meta):
                    continue

                # Star range filter
                if meta.stars < settings.repo_min_stars or meta.stars > settings.repo_max_stars:
                    continue

                # Skip repos already cached with valid signals
                if await _has_valid_cache(full_name, db):
                    continue

                # Skip banned repos
                if await db.is_repo_banned(full_name):
                    logger.debug("repo_banned_skip", repo=full_name)
                    continue

                candidates.append(meta)

    logger.info("repos_found", count=len(candidates), searched=len(seen))
    return candidates


async def _search_batch(
    github: GitHubCLIProtocol,
    language: str,
    keyword: str,
    pushed_qualifier: str,
) -> list[dict[str, Any]]:
    """Run a single ``gh search repos`` call and return parsed results."""
    # repositoryTopics is not available in gh search results — use available fields
    result = await github.run_gh([
        "search", "repos",
        "--language", language,
        "--sort", "updated",
        "--order", "desc",
        "--limit", "30",
        "--json", "fullName,description,language,stargazersCount,pushedAt,isArchived",
        "--",
        f"topic:{keyword}",
        f"stars:{settings.repo_min_stars}..{settings.repo_max_stars}",
        pushed_qualifier,
    ])

    if not result.success:
        logger.debug("search_failed", language=language, keyword=keyword, stderr=result.stderr[:200])
        return []

    try:
        data: list[dict[str, Any]] = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return []

    # Filter out archived repos
    return [r for r in data if not r.get("isArchived", False)]


def _to_repo_meta(data: dict[str, Any], keyword: str = "") -> RepoMeta | None:
    """Convert a ``gh search repos`` JSON record to a ``RepoMeta``.

    The ``keyword`` parameter is the topic: qualifier used in the search.
    Since ``gh search repos`` doesn't return repositoryTopics in results,
    we use the search keyword as the known topic.
    """
    full_name = data.get("fullName", "")
    if "/" not in full_name:
        return None

    owner, name = full_name.split("/", 1)
    language = data.get("language") or ""
    stars = data.get("stargazersCount", 0)
    description = data.get("description") or ""

    # We know the repo matched topic:{keyword} in the search, so include it
    topics = [keyword] if keyword else []

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
