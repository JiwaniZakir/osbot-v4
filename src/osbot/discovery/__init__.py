"""osbot.discovery -- find repos and issues worth contributing to.

Re-exports the top-level ``discover`` coroutine that orchestrates the
full discovery pipeline: find repos, compute signals, score repos,
find issues, score issues.  Zero Claude calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from typing import Any

from osbot.config import settings
from osbot.discovery.issue_finder import find_issues
from osbot.discovery.issue_scorer import score_issue
from osbot.discovery.repo_finder import find_repos
from osbot.discovery.repo_scorer import score_repo
from osbot.discovery.repo_signals import compute_signals
from osbot.intel.graphql import GraphQLClient
from osbot.log import get_logger
from osbot.types import GitHubCLIProtocol, MemoryDBProtocol, RepoMeta, ScoredIssue

logger = get_logger(__name__)


async def discover(
    github: GitHubCLIProtocol,
    db: MemoryDBProtocol,
) -> list[ScoredIssue]:
    """Run the full discovery pipeline.

    Steps:
    1. Search GitHub for candidate repos (``repo_finder``).
    2. Compute external signals for each candidate (``repo_signals``).
    3. Score repos and filter to active pool (``repo_scorer``).
    4. Search active-pool repos for open issues (``issue_finder``).
    5. Score and rank issues (``issue_scorer``).

    Args:
        github: CLI protocol for all GitHub operations.
        db: Memory database for caching and outcome lookups.

    Returns:
        List of ``ScoredIssue`` sorted by score descending.
    """
    # Step 1: Find candidate repos
    candidates = await find_repos(github, db)
    logger.info("discovery_candidates", count=len(candidates))

    # Step 2 & 3: Compute signals and score, build active pool.
    # Rate-limit signal computation: max 50 repos per cycle to avoid
    # burning GitHub's API budget. Remaining repos get computed next cycle.
    MAX_SIGNALS_PER_CYCLE = 50
    active_pool: list[RepoMeta] = []
    for signals_computed, candidate in enumerate(candidates):
        if signals_computed >= MAX_SIGNALS_PER_CYCLE:
            logger.info("signals_rate_limited", computed=signals_computed,
                        remaining=len(candidates) - signals_computed)
            break

        signals = await compute_signals(candidate, github, db)

        # Small delay between signal computations (each makes 2-3 API calls)
        await asyncio.sleep(1.0)

        repo_score = score_repo(candidate, signals)

        if repo_score >= settings.repo_score_threshold:
            enriched = RepoMeta(
                owner=candidate.owner,
                name=candidate.name,
                language=candidate.language,
                stars=candidate.stars,
                description=candidate.description,
                topics=candidate.topics,
                has_contributing=candidate.has_contributing,
                requires_assignment=candidate.requires_assignment,
                has_ai_policy=candidate.has_ai_policy,
                ci_enabled=signals.get("has_ci", False),
                external_merge_rate=signals.get("external_merge_rate", 0.0),
                avg_response_hours=signals.get("avg_response_hours", 0.0),
                close_completion_rate=signals.get("close_completion_rate", 0.0),
                score=repo_score,
                last_push_at=candidate.last_push_at,
            )
            active_pool.append(enriched)

        if len(active_pool) >= settings.active_pool_max:
            break

    logger.info("discovery_active_pool", count=len(active_pool))

    # Step 4: Supplement active pool from cached repo_signals in the DB.
    # When all search results are already cached (repos_found=0), the active_pool
    # built above is empty and find_issues gets no repos.  Fix: load existing
    # high-scoring repos from the DB and randomly sample up to active_pool_max.
    if len(active_pool) < settings.active_pool_max:
        db_repos = await _load_active_pool_from_db(db)
        new_names = {r.full_name for r in active_pool}
        for repo in db_repos:
            if repo.full_name not in new_names:
                active_pool.append(repo)
                new_names.add(repo.full_name)
        # Shuffle so we rotate through the full DB pool across cycles.
        random.shuffle(active_pool)
        active_pool = active_pool[:settings.active_pool_max]
        logger.info("discovery_pool_after_db_supplement", count=len(active_pool))

    # Step 5: Find issues in active pool
    graphql_client = GraphQLClient(github)
    raw_issues = await find_issues(active_pool, github, graphql_client, db)

    # Step 6: Score issues
    # Fetch outcomes and lessons for scoring context
    db_state = await _load_scoring_context(db)

    scored: list[ScoredIssue] = []
    for issue_data in raw_issues:
        repo_meta = issue_data.get("repo_meta")
        if repo_meta is None:
            # Fallback: find matching repo in active pool
            repo_name = issue_data.get("repo", "")
            repo_meta = next(
                (r for r in active_pool if r.full_name == repo_name), None
            )
        if repo_meta is None:
            continue

        scored_issue = score_issue(issue_data, repo_meta, db_state)
        scored.append(scored_issue)

    # Sort by score descending
    scored.sort(key=lambda si: si.score, reverse=True)

    logger.info(
        "discovery_complete",
        issues_scored=len(scored),
        top_score=scored[0].score if scored else 0.0,
    )
    return scored


async def _load_active_pool_from_db(db: MemoryDBProtocol) -> list[RepoMeta]:
    """Load high-scoring repos from repo_signals for issue discovery.

    Reconstructs minimal RepoMeta objects from cached signals so that
    find_issues() can search repos already in the DB without needing a new
    search cycle to re-discover them.  Called when find_repos() returns no
    new candidates (all repos already cached).
    """
    try:
        rows = await db.fetchall(
            """
            SELECT repo, requires_assignment, has_ai_policy, ci_enabled,
                   external_merge_rate, avg_response_hours, close_completion_rate
            FROM repo_signals
            WHERE expires_at > datetime('now')
              AND (has_ai_policy IS NULL OR has_ai_policy = 0)
            ORDER BY external_merge_rate DESC
            LIMIT ?
            """,
            (settings.active_pool_max * 4,),
        )
    except Exception as exc:
        logger.debug("load_active_pool_from_db_failed", error=str(exc))
        return []

    result: list[RepoMeta] = []
    for row in rows:
        repo_full = row.get("repo", "")
        if "/" not in repo_full:
            continue
        owner, name = repo_full.split("/", 1)

        emr = float(row.get("external_merge_rate") or 0)
        resp = float(row.get("avg_response_hours") or 999)
        cc = float(row.get("close_completion_rate") or 0)
        ci = bool(row.get("ci_enabled"))

        # Recompute score using same formula as repo_scorer to filter by threshold.
        score = 5.0
        if emr > 0.40:
            score += 3.0
        elif emr > 0.20:
            score += 1.5
        elif emr < 0.05:
            score -= 3.0
        if resp < 24:
            score += 1.0
        elif resp < 72:
            score += 0.5
        elif resp > 168:
            score -= 1.0
        if cc > 0.60:
            score += 0.5
        elif cc < 0.20:
            score -= 0.5
        if ci:
            score += 0.5

        if score < settings.repo_score_threshold:
            continue

        result.append(RepoMeta(
            owner=owner,
            name=name,
            language="",
            stars=0,
            description="",
            topics=[],
            requires_assignment=bool(row.get("requires_assignment")),
            has_ai_policy=False,
            ci_enabled=ci,
            external_merge_rate=emr,
            avg_response_hours=resp,
            close_completion_rate=cc,
            score=score,
        ))

    return result


async def _load_scoring_context(db: MemoryDBProtocol) -> dict[str, Any]:
    """Load outcomes and lessons from the DB for issue scoring."""
    outcomes: list[dict[str, Any]] = []
    lessons: list[dict[str, Any]] = []

    try:
        outcomes = await db.fetchall(
            "SELECT repo, issue_number, outcome, labels FROM outcomes ORDER BY created_at DESC LIMIT 200"
        )
    except Exception:
        # Table may not exist yet or have different schema
        pass

    with contextlib.suppress(Exception):
        lessons = await db.fetchall(
            "SELECT repo, key, value, source FROM repo_facts WHERE key LIKE 'lesson_%' ORDER BY created_at DESC LIMIT 100"
        )

    scope_pass_rates: dict[str, float] = {}
    try:
        scope_rows = await db.fetchall(
            """
            SELECT repo,
                   SUM(scope_correct) as passed,
                   COUNT(*) as total
            FROM phase_checkpoints
            GROUP BY repo
            HAVING COUNT(*) >= 5
            """,
        )
        for row in scope_rows:
            total = row.get("total", 0) or 0
            passed = row.get("passed", 0) or 0
            if total >= 5:
                scope_pass_rates[row.get("repo", "")] = passed / total
    except Exception:
        pass  # phase_checkpoints may not exist yet

    return {"outcomes": outcomes, "lessons": lessons, "scope_pass_rates": scope_pass_rates}


__all__ = [
    "discover",
    "find_repos",
    "find_trending_repos",
    "compute_signals",
    "score_repo",
    "find_issues",
    "score_issue",
]
