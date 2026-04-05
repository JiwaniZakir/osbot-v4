"""Issue finder -- efficient batched issue discovery.

Uses TWO strategies to minimize GitHub API calls:

Strategy 1 — Label-based via ``gh issue list`` (REST, NOT search API):
  One call per repo with comma-joined labels. This uses the Issues API
  (5000 req/hr) not the Search API (30 req/min).

Strategy 2 — GraphQL batch enrichment:
  One GraphQL call per issue for comments, timeline, reactions.
  GraphQL has its own 5000 points/hr budget, separate from REST.

We deliberately AVOID ``gh search issues`` for per-repo keyword search
because it hits the Search API rate limit (30/min). Instead, we do
keyword filtering client-side on the label results.

API budget per cycle (100 repos):
  ~100 REST calls (gh issue list, 1 per repo)
  ~200 GraphQL calls (enrichment, ~2 issues/repo average)
  Total: ~300 calls, well within 5000/hr REST + 5000 points/hr GraphQL.

Zero Claude calls.  Layer 4.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from osbot.intel.duplicates import check_claimed_in_comments
from osbot.log import get_logger

# Issues rejected more than this many days ago are allowed back into the queue.
# Must match the equivalent window in pipeline/preflight.py.
_OUTCOME_RETRY_DAYS = 7
_TERMINAL_OUTCOMES = frozenset({"submitted", "merged", "iterated_merged"})

if TYPE_CHECKING:
    from osbot.intel.graphql import GraphQLClient
    from osbot.types import GitHubCLIProtocol, MemoryDBProtocol, RepoMeta

logger = get_logger(__name__)

# Labels that indicate tractable issues — comma-joined in a single API call.
_TARGET_LABELS: list[str] = ["bug", "help wanted", "good first issue"]

# Keywords for client-side filtering (applied to title + body after fetching).
_QUALITY_KEYWORDS: list[str] = [
    "typo",
    "broken link",
    "missing import",
    "deprecat",
    "wrong type",
    "incorrect",
    "error message",
    "regression",
    "unused",
    "dead code",
    "fix",
    "null",
    "crash",
    "exception",
]

# Freshness thresholds.
_MAX_CREATED_DAYS = 90
_MAX_UPDATED_DAYS = 30

# Concurrency for GraphQL enrichment (avoid overwhelming the API).
_ENRICH_CONCURRENCY = 5

# Max issues to enrich per repo (don't spend all budget on one repo).
_MAX_ISSUES_PER_REPO = 5

# Minimum pause between REST calls to be respectful.
_REST_DELAY_SEC = 0.5


async def find_issues(
    repos: list[RepoMeta],
    github: GitHubCLIProtocol,
    graphql: GraphQLClient,
    db: MemoryDBProtocol,
) -> list[dict[str, Any]]:
    """Search for open issues across repos, enriched via GraphQL.

    Strategy:
    1. For each repo: ONE ``gh issue list`` call with target labels (REST API).
    2. Client-side keyword filtering on the results.
    3. Deduplicate by repo#number across the full result set.
    4. Skip issues we already have outcomes for.
    5. Freshness filter.
    6. GraphQL enrichment (comments, timeline, reactions) with concurrency limit.

    This uses ~1 REST call per repo + ~2 GraphQL calls per repo = ~300 total
    for 100 repos, staying well within GitHub's rate limits.
    """
    all_issues: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Phase 1: Fetch candidates from all repos (REST, 1 call per repo)
    raw_candidates: list[tuple[RepoMeta, dict[str, Any]]] = []

    for repo in repos:
        issues = await _fetch_repo_issues(repo.full_name, github)
        for issue in issues:
            number = issue.get("number", 0)
            key = f"{repo.full_name}#{number}"
            if key in seen:
                continue
            seen.add(key)

            # Skip if we already have a terminal or recent outcome.
            # Terminal outcomes (submitted/merged) are permanent.
            # Rejected/stuck outcomes expire after _OUTCOME_RETRY_DAYS so the
            # issue can be re-queued and retried (matches preflight logic).
            existing = await db.get_outcome(repo.full_name, number)
            if existing is not None:
                prev_outcome = existing.get("outcome", "")
                if prev_outcome in _TERMINAL_OUTCOMES:
                    continue
                # For rejected/stuck: only skip if the outcome is recent
                created_str = existing.get("created_at", "") or ""
                try:
                    created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    age_days = (datetime.now(UTC) - created_at).days
                except (ValueError, TypeError):
                    age_days = 0
                if age_days < _OUTCOME_RETRY_DAYS:
                    continue
                # Outcome is stale — allow re-discovery

            # Freshness filter
            if not _is_fresh(issue):
                continue

            raw_candidates.append((repo, issue))

        # Small delay between repos to be respectful
        await asyncio.sleep(_REST_DELAY_SEC)

    logger.info("issue_candidates", count=len(raw_candidates), repos_searched=len(repos))

    # Phase 2: Score candidates client-side to pick the best ones for enrichment
    # (enrichment is the expensive part — GraphQL call per issue)
    scored_candidates = _prescore_candidates(raw_candidates)

    # Phase 3: Enrich top candidates via GraphQL (concurrent but limited)
    semaphore = asyncio.Semaphore(_ENRICH_CONCURRENCY)

    async def _enrich_one(repo: RepoMeta, issue_data: dict[str, Any]) -> dict[str, Any] | None:
        async with semaphore:
            return await _enrich_issue(repo, issue_data.get("number", 0), issue_data, graphql)

    tasks = [_enrich_one(repo, issue) for repo, issue in scored_candidates]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, dict):
            all_issues.append(result)
        elif isinstance(result, Exception):
            logger.debug("enrich_error", error=str(result))

    logger.info("issues_found", count=len(all_issues), repos_searched=len(repos))
    return all_issues


async def _fetch_repo_issues(repo: str, github: GitHubCLIProtocol) -> list[dict[str, Any]]:
    """Fetch open issues from a repo using ONE ``gh issue list`` call.

    Uses the Issues API (5000/hr limit), NOT the Search API (30/min limit).
    Fetches issues with ANY of the target labels in a single call.
    """
    # Fetch open issues with target labels. Use separate --label calls
    # for each label since gh doesn't support OR logic in --search for labels.
    # But we can use --search with GitHub's search syntax for OR.
    # "label:bug label:\"help wanted\"" is AND in GitHub search.
    # Instead, fetch recent open issues (no label filter) and filter client-side.
    # This is 1 API call per repo and gives us the broadest coverage.
    result = await github.run_gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "30",
            "--json",
            "number,title,body,labels,url,createdAt,updatedAt",
        ]
    )

    if not result.success:
        logger.debug("issue_fetch_failed", repo=repo, stderr=result.stderr[:200])
        return []

    try:
        issues: list[dict[str, Any]] = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return []

    return issues


def _prescore_candidates(candidates: list[tuple[RepoMeta, dict[str, Any]]]) -> list[tuple[RepoMeta, dict[str, Any]]]:
    """Quick client-side scoring to pick the best candidates for enrichment.

    This avoids spending GraphQL budget on low-quality issues.
    Filters out feature/enhancement labels before enrichment to save API calls.
    Returns at most ``_MAX_ISSUES_PER_REPO`` per repo, sorted by quality signals.
    """
    # Pre-filter: drop issues with feature/enhancement labels before enrichment.
    # These never produce a mergeable minimal fix and waste GraphQL budget.
    _SKIP_LABELS = {"feature", "enhancement", "proposal", "rfc", "feature request", "feature-request"}

    filtered: list[tuple[RepoMeta, dict[str, Any]]] = []
    skipped = 0
    for repo, issue in candidates:
        labels = {
            (label.get("name", "") if isinstance(label, dict) else str(label)).lower()
            for label in issue.get("labels", [])
        }
        if labels & _SKIP_LABELS:
            skipped += 1
            continue
        filtered.append((repo, issue))

    if skipped:
        logger.info("prescore_feature_filtered", skipped=skipped)

    # Group by repo
    by_repo: dict[str, list[tuple[RepoMeta, dict[str, Any], float]]] = {}
    for repo, issue in filtered:
        quick_score = _quick_score(issue)
        key = repo.full_name
        if key not in by_repo:
            by_repo[key] = []
        by_repo[key].append((repo, issue, quick_score))

    # Take top N per repo
    result: list[tuple[RepoMeta, dict[str, Any]]] = []
    for _key, items in by_repo.items():
        items.sort(key=lambda x: x[2], reverse=True)
        for repo, issue, _score in items[:_MAX_ISSUES_PER_REPO]:
            result.append((repo, issue))

    return result


def _quick_score(issue: dict[str, Any]) -> float:
    """Quick client-side scoring without GraphQL enrichment.

    Checks title + body for quality keywords, label matches, and basic signals.
    Also applies negative scores for feature/investigation issues that produce
    empty diffs 80%+ of the time.
    """
    score = 0.0
    title = (issue.get("title") or "").lower()
    body = (issue.get("body") or "").lower()
    text = title + " " + body

    # Keyword match in title is strong signal
    for kw in _QUALITY_KEYWORDS:
        if kw in title:
            score += 2.0
            break
        if kw in body:
            score += 0.5

    # Has code block (actionable)
    if "```" in body:
        score += 1.0

    # Has error trace (clear failure)
    if any(marker in text for marker in ("traceback", "error:", "exception:", "stack trace")):
        score += 1.5

    # Good labels
    labels = {
        (label.get("name", "") if isinstance(label, dict) else str(label)).lower() for label in issue.get("labels", [])
    }
    if "bug" in labels:
        score += 1.0
    if "good first issue" in labels:
        score += 0.5
    if "help wanted" in labels:
        score += 0.5

    # Body length sweet spot (not too short, not too long)
    body_len = len(issue.get("body") or "")
    if 200 <= body_len <= 2000:
        score += 0.5
    elif body_len < 50:
        score -= 1.0

    # --- Negative scores for non-implementable issues ---

    # Feature/enhancement labels: these never produce a mergeable minimal fix
    _FEATURE_LABELS_QUICK = {"feature", "enhancement", "proposal", "rfc", "feature request", "feature-request"}
    if labels & _FEATURE_LABELS_QUICK:
        score -= 3.0

    # Investigation/research keywords in title: strong signal of non-implementability
    _INVESTIGATION_TITLE_KW = {
        "investigate",
        "research",
        "explore",
        "understand",
        "analyze",
        "analysis",
        "look into",
        "figure out",
    }
    if any(kw in title for kw in _INVESTIGATION_TITLE_KW):
        score -= 2.0

    # "Add support for", "implement X", "design" in title: feature request phrasing
    if any(kw in title for kw in ("add support for", "implement ", "design ", "redesign ")):
        score -= 2.0

    return score


def _is_fresh(issue_data: dict[str, Any]) -> bool:
    """Return True if the issue meets freshness thresholds."""
    now = datetime.now(UTC)

    created_str = issue_data.get("createdAt", "")
    if created_str:
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            if (now - created) > timedelta(days=_MAX_CREATED_DAYS):
                return False
        except (ValueError, TypeError):
            pass

    updated_str = issue_data.get("updatedAt", "")
    if updated_str:
        try:
            updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            if (now - updated) > timedelta(days=_MAX_UPDATED_DAYS):
                return False
        except (ValueError, TypeError):
            pass

    return True


async def _enrich_issue(
    repo: RepoMeta,
    number: int,
    base_data: dict[str, Any],
    graphql: GraphQLClient,
) -> dict[str, Any] | None:
    """Enrich an issue with GraphQL data (comments, timeline, reactions)."""
    try:
        detail = await graphql.issue_detail(repo.owner, repo.name, number)
    except RuntimeError:
        logger.debug("enrich_failed", repo=repo.full_name, issue=number)
        return None

    if not detail:
        return None

    # Extract labels
    labels: list[str] = [node.get("name", "") for node in detail.get("labels", {}).get("nodes", [])]

    # Detect maintainer confirmation from comments
    maintainer_confirmed = False
    comments = detail.get("comments", {}).get("nodes", [])
    comment_count = len(comments)
    for comment in comments:
        assoc = (comment.get("authorAssociation") or "").upper()
        if assoc in ("MEMBER", "OWNER", "COLLABORATOR"):
            body_lower = (comment.get("body") or "").lower()
            if any(
                kw in body_lower
                for kw in (
                    "confirmed",
                    "reproduced",
                    "can reproduce",
                    "this is a bug",
                    "valid bug",
                    "good catch",
                    "yes, this is",
                    "verified",
                    "can confirm",
                )
            ):
                maintainer_confirmed = True
                break

    # Early filter: skip issues claimed by another contributor in comments
    claimed, claimer = check_claimed_in_comments(comments)
    if claimed:
        logger.info(
            "issue_skipped_claimed",
            repo=repo.full_name,
            issue=number,
            claimer=claimer,
        )
        return None

    # Detect error traces and code blocks in body
    body = detail.get("body") or base_data.get("body", "")
    has_error_trace = bool(
        "Traceback" in body or "Error:" in body or "Exception:" in body or "stack trace" in body.lower()
    )
    has_code_block = "```" in body

    # Reaction count
    reaction_count = detail.get("reactions", {}).get("totalCount", 0)

    return {
        "repo": repo.full_name,
        "number": number,
        "title": detail.get("title") or base_data.get("title", ""),
        "body": body,
        "labels": labels,
        "url": base_data.get("url", ""),
        "created_at": detail.get("createdAt") or base_data.get("createdAt", ""),
        "updated_at": detail.get("updatedAt") or base_data.get("updatedAt", ""),
        "comment_count": comment_count,
        "reaction_count": reaction_count,
        "maintainer_confirmed": maintainer_confirmed,
        "has_error_trace": has_error_trace,
        "has_code_block": has_code_block,
        "requires_assignment": repo.requires_assignment,
        "repo_meta": repo,
    }
