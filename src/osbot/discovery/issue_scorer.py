"""Issue scorer -- v2 formula with seven additive adjustments.

score = 5.0 + repo_adj + label_adj + quality_adj + lesson_adj + benchmark_adj
        + implementability_adj + scope_adj + age_adj, clamped 1-10.

Includes negative adjustments for feature requests, investigations, and
discussion-style issues that produce empty diffs 80%+ of the time.

All arithmetic, no Claude calls, no ML.  Layer 4.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from osbot.config import settings
from osbot.log import get_logger
from osbot.types import RepoMeta, ScoredIssue

logger = get_logger(__name__)


def score_issue(
    issue_data: dict[str, Any],
    repo_meta: RepoMeta,
    db_state: dict[str, Any] | None = None,
) -> ScoredIssue:
    """Score an enriched issue using the v2 six-adjustment formula.

    Scoring breakdown:
    - Base: ``settings.issue_base_score`` (default 5.0)
    - ``repo_adj`` [-2.0, +2.0]: Blended merge rate for this repo.
    - ``label_adj`` [-1.5, +1.0]: Label-level merge rate from outcomes.
    - ``quality_adj`` [0.0, +5.3]: Maintainer confirmed (+1.50), error trace
      (+0.50), regression label (+0.50), cleanup/removal (+2.00),
      typo/docs (+1.00), good first issue (+0.80), code block (+0.30),
      comment count 1-5 (+0.40), reactions >=5 (+0.10).
    - ``lesson_adj`` [-3.0, 0.0]: Negative lessons for this repo from memory.
    - ``implementability_adj`` [-3.5, 0.0]: Penalizes feature requests (-2.0),
      investigation tasks (-1.5), discussions without bug label (-1.0),
      and long prose with no code/error trace (-1.0).
    - ``age_adj`` [-0.5, +0.5]: Penalizes issues < 3 days old (likely user
      errors or duplicates, no community confirmation yet), rewards mature
      issues > 14 days old (community confirmed, maintainer aware).

    Args:
        issue_data: Enriched issue dict from ``issue_finder``.
        repo_meta: Repository metadata with signal fields.
        db_state: Optional dict with ``outcomes`` (list of past outcomes)
                  and ``lessons`` (list of lesson dicts) for scoring context.

    Returns:
        A ``ScoredIssue`` with the computed score.
    """
    state = db_state or {}

    base = settings.issue_base_score
    repo_adj = _compute_repo_adj(repo_meta, state.get("outcomes", []))
    label_adj = _compute_label_adj(issue_data.get("labels", []), state.get("outcomes", []))
    quality_adj = _compute_quality_adj(issue_data)
    lesson_adj = _compute_lesson_adj(state.get("lessons", []))
    benchmark_adj = _compute_benchmark_adj(issue_data, state.get("benchmarks", {}))
    implementability_adj = _compute_implementability_adj(issue_data)
    scope_adj = _compute_scope_adj(issue_data.get("repo", ""), state.get("scope_pass_rates", {}))
    age_adj = _compute_age_adj(issue_data.get("created_at", ""))

    raw = base + repo_adj + label_adj + quality_adj + lesson_adj + benchmark_adj + implementability_adj + scope_adj + age_adj
    score = max(1.0, min(10.0, raw))
    score = round(score, 2)

    logger.debug(
        "issue_scored",
        repo=issue_data.get("repo", ""),
        number=issue_data.get("number", 0),
        score=score,
        base=base,
        repo_adj=round(repo_adj, 2),
        label_adj=round(label_adj, 2),
        quality_adj=round(quality_adj, 2),
        lesson_adj=round(lesson_adj, 2),
        benchmark_adj=round(benchmark_adj, 2),
        implementability_adj=round(implementability_adj, 2),
        scope_adj=round(scope_adj, 2),
        age_adj=round(age_adj, 2),
    )

    return ScoredIssue(
        repo=issue_data.get("repo", ""),
        number=issue_data.get("number", 0),
        title=issue_data.get("title", ""),
        body=issue_data.get("body", ""),
        labels=issue_data.get("labels", []),
        url=issue_data.get("url", ""),
        score=score,
        maintainer_confirmed=issue_data.get("maintainer_confirmed", False),
        has_error_trace=issue_data.get("has_error_trace", False),
        has_code_block=issue_data.get("has_code_block", False),
        requires_assignment=issue_data.get("requires_assignment", False),
        created_at=issue_data.get("created_at", ""),
        updated_at=issue_data.get("updated_at", ""),
        comment_count=issue_data.get("comment_count", 0),
        reaction_count=issue_data.get("reaction_count", 0),
    )


# ------------------------------------------------------------------
# Adjustment functions
# ------------------------------------------------------------------


def _compute_repo_adj(
    repo_meta: RepoMeta,
    outcomes: list[dict[str, Any]],
) -> float:
    """Repo adjustment [-2.0, +2.0] based on blended merge rate.

    For repos with bot history, blends our actual merge rate (70% weight)
    with the external merge rate (30% weight).  For new repos (no history),
    uses only the external rate.
    """
    ext_rate = repo_meta.external_merge_rate

    # Filter outcomes to this repo
    repo_outcomes = [o for o in outcomes if o.get("repo") == repo_meta.full_name]

    if repo_outcomes:
        our_merged = sum(1 for o in repo_outcomes if o.get("outcome") == "merged")
        our_total = len(repo_outcomes)
        our_rate = our_merged / our_total if our_total > 0 else 0.0
        blended = 0.7 * our_rate + 0.3 * ext_rate
    else:
        blended = ext_rate

    # Map blended rate to adjustment
    if blended >= 0.50:
        return 2.0
    elif blended >= 0.30:
        return 1.0
    elif blended >= 0.10:
        return 0.0
    elif blended >= 0.05:
        return -1.0
    else:
        return -2.0


def _compute_label_adj(
    labels: list[str],
    outcomes: list[dict[str, Any]],
) -> float:
    """Label adjustment [-1.5, +1.0] based on label-level merge rate.

    Uses historical outcomes for issues with the same labels.
    Threshold lowered from 3 to 2 samples so bad label patterns are caught
    sooner, with a stronger -1.5 penalty (was -1.0) since 2 consecutive
    failures on a label type is already a meaningful signal.
    """
    if not outcomes or not labels:
        return 0.0

    label_set = {lbl.lower() for lbl in labels}
    matching = [
        o for o in outcomes
        if label_set & {lbl.lower() for lbl in o.get("labels", [])}
    ]

    if len(matching) < 2:
        # Need at least 2 outcomes for a label-level signal
        return 0.0

    merged = sum(1 for o in matching if o.get("outcome") == "merged")
    rate = merged / len(matching)

    if rate >= 0.50:
        return 1.0
    elif rate >= 0.25:
        return 0.5
    elif rate < 0.10:
        return -1.5  # Strengthened from -1.0 (2 failures is a real pattern)
    else:
        return 0.0


def _compute_quality_adj(issue_data: dict[str, Any]) -> float:
    """Quality adjustment [0.0, +5.3] based on issue quality signals."""
    adj = 0.0

    # Maintainer confirmed: strongest single signal
    if issue_data.get("maintainer_confirmed"):
        adj += settings.maintainer_confirmed_bonus  # 1.50

    # Error trace present: easier to understand and fix
    if issue_data.get("has_error_trace"):
        adj += 0.50

    # Regression label
    labels_lower = {lbl.lower() for lbl in issue_data.get("labels", [])}
    if "regression" in labels_lower:
        adj += 0.50

    # Cleanup/removal tasks: CCA data shows 84.7% merge rate -- highest category
    _CLEANUP_LABELS = {"cleanup", "chore", "refactor", "removal", "dead code", "unused"}
    _CLEANUP_TITLE_KW = {"remove", "delete", "deprecate", "clean up", "unused", "dead"}
    _CLEANUP_BODY_KW = {"can be removed", "no longer needed", "deprecated", "obsolete"}

    title_lower = (issue_data.get("title") or "").lower()
    body_lower = (issue_data.get("body") or "").lower()

    is_cleanup = bool(labels_lower & _CLEANUP_LABELS)
    if not is_cleanup:
        # Check for partial label matches (e.g. "deprecation" matches "deprecat")
        is_cleanup = any("deprecat" in lbl for lbl in labels_lower)
    if not is_cleanup:
        is_cleanup = any(kw in title_lower for kw in _CLEANUP_TITLE_KW)
    if not is_cleanup:
        is_cleanup = any(kw in body_lower for kw in _CLEANUP_BODY_KW)

    if is_cleanup:
        adj += 2.00  # Raised from 1.50 -- CCA data shows 84.7% merge rate, highest category

    # Typo/docs issues: high merge rate, low risk
    _DOCS_LABELS = {"typo", "documentation", "docs", "spelling"}
    is_docs = bool(labels_lower & _DOCS_LABELS)
    if not is_docs:
        is_docs = any(kw in title_lower for kw in ("typo", "spelling", "documentation fix"))

    if is_docs and not is_cleanup:  # Don't double-count
        adj += 1.00

    # "good first issue" label: repos specifically mark these as high-priority,
    # low-complexity tasks with well-defined scope. 70%+ merge rate in practice.
    _GFI_LABELS = {"good first issue", "good-first-issue", "beginner", "starter",
                   "first-timers-only", "first timers only", "newcomer"}
    if labels_lower & _GFI_LABELS:
        adj += 0.80

    # Code block in body: concrete example
    if issue_data.get("has_code_block"):
        adj += 0.30

    # Comment count sweet spot: 1-5 comments = engagement without controversy
    comment_count = issue_data.get("comment_count", 0)
    if 1 <= comment_count <= 5:
        adj += 0.40

    # Community reactions: weak signal (emotional issues also get reactions),
    # downweighted from +0.30 to +0.10
    reaction_count = issue_data.get("reaction_count", 0)
    if reaction_count >= 5:
        adj += 0.10

    return min(adj, 5.3)  # Cap raised to accommodate new cleanup (+2.0) + gfi (+0.8)


def _compute_lesson_adj(lessons: list[dict[str, Any]]) -> float:
    """Lesson adjustment [-3.0, 0.0] based on negative lessons in memory.

    Each negative lesson applies a -0.75 penalty, capped at -3.0.
    """
    if not lessons:
        return 0.0

    negative_count = sum(
        1 for lesson in lessons
        if lesson.get("sentiment", "negative") == "negative"
        or lesson.get("type") == "negative"
    )

    penalty = -0.75 * negative_count
    return max(-3.0, penalty)


def _compute_benchmark_adj(
    issue_data: dict[str, Any],
    benchmarks: dict[str, Any],
) -> float:
    """Benchmark adjustment [0.0, +0.5] from contributor pattern study.

    Restored from v3's contributor_benchmark.py. If the repo has benchmark
    data (top contributor patterns studied via ``gh`` CLI), boost issues
    that match successful contribution patterns:
    - Issue involves a small fix (matches typical PR size of top contributors)
    - Issue area matches labels top contributors work on
    - Issue includes test expectations (top contributors add tests)

    Args:
        issue_data: Enriched issue dict.
        benchmarks: Dict keyed by repo with benchmark stats, e.g.
            ``{"avg_pr_lines": 30, "test_inclusion_rate": 0.8, "common_labels": ["bug"]}``.

    Returns:
        Bonus 0.0 to +0.5.
    """
    repo = issue_data.get("repo", "")
    bench = benchmarks.get(repo)
    if not bench:
        return 0.0

    bonus = 0.0

    # Issue is a bug fix and top contributors mostly fix bugs
    labels_lower = {lbl.lower() for lbl in issue_data.get("labels", [])}
    common_labels = {lbl.lower() for lbl in bench.get("common_labels", [])}
    if labels_lower & common_labels:
        bonus += 0.20

    # Issue has error trace/code block (matches test-heavy contributor pattern)
    test_rate = bench.get("test_inclusion_rate", 0.0)
    if test_rate > 0.6 and (issue_data.get("has_error_trace") or issue_data.get("has_code_block")):
        bonus += 0.15

    # Issue body length suggests a small, well-scoped fix
    body_len = len(issue_data.get("body", ""))
    avg_pr = bench.get("avg_pr_lines", 100)
    if 100 < body_len < 2000 and avg_pr < 60:
        bonus += 0.15

    return min(bonus, 0.8)  # Raised cap from 0.5 -- contributor patterns are strong signals


def _compute_scope_adj(repo: str, scope_pass_rates: dict) -> float:
    """Penalize repos where Claude consistently fails the scope gate.

    Per-repo scope pass rate from phase_checkpoints. Repos where
    Claude can't produce in-scope fixes should be deprioritized.

    Returns adjustment in range [-3.0, 0.0].
    """
    if not scope_pass_rates or repo not in scope_pass_rates:
        return 0.0
    rate = scope_pass_rates[repo]
    if rate < 0.05:   # < 5% scope pass rate -- consistently over-scoped
        return -3.0   # Raised from -2.0: deprioritize much more aggressively
    if rate < 0.15:   # < 15%
        return -1.0
    if rate < 0.25:   # < 25%
        return -0.5
    return 0.0


def _compute_age_adj(created_at: str) -> float:
    """Issue age adjustment [-0.5, +0.5] based on days since issue was opened.

    Fresh issues (<3 days) often haven't had time to:
      - Receive maintainer confirmation
      - Accumulate community engagement
      - Distinguish themselves from duplicates or user errors

    Mature issues (>14 days) have survived the initial noise period and
    are more likely to be genuine, confirmed bugs worth fixing.

    Returns adjustment in range [-0.5, +0.5].
    """
    if not created_at:
        return 0.0
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        age_days = (datetime.now(UTC) - created).days
    except (ValueError, TypeError):
        return 0.0

    if age_days < 3:
        return -0.5   # Too fresh: likely user error, duplicate, or unconfirmed
    if age_days > 14:
        return 0.5    # Mature issue: survived first-week noise, maintainer-aware
    return 0.0        # 3-14 days: neutral


# ------------------------------------------------------------------
# Implementability filter (negative adjustments)
# ------------------------------------------------------------------

# Labels that indicate feature requests / proposals -- NOT implementable as a minimal fix
_FEATURE_LABELS = {"feature", "enhancement", "proposal", "rfc", "feature request", "feature-request"}

# Title/body keywords that indicate investigation / research issues
_INVESTIGATION_KW = {
    "investigate", "research", "explore why", "understand why",
    "analyze", "analysis", "figure out", "look into",
    "deep dive", "root cause analysis",
}

# Labels for discussion / question issues (penalty only if NOT also a bug)
# PM review: removed "help wanted" — it's a primary search target for the bot
# and often paired with "good first issue" on actionable bugs/typos.
_DISCUSSION_LABELS = {"discussion", "question", "needs triage", "needs-triage"}


def _compute_implementability_adj(issue_data: dict[str, Any]) -> float:
    """Implementability adjustment [-3.5, 0.0] -- penalize non-implementable issues.

    Issues that are feature requests, investigation tasks, or long design
    discussions produce empty diffs 80%+ of the time.  Penalize them heavily
    so the scorer prefers concrete, fixable bugs.

    Penalties (cumulative, capped at -3.5):
    - Feature request labels (feature, enhancement, proposal, RFC): -2.0
    - Investigation keywords in title/body: -1.5
    - Discussion labels without bug label: -1.0
    - Long prose with no error trace and no code block: -1.0
    """
    penalty = 0.0

    labels_lower = {lbl.lower() for lbl in issue_data.get("labels", [])}
    title_lower = (issue_data.get("title") or "").lower()
    body_lower = (issue_data.get("body") or "").lower()

    # 1. Feature request labels -> -2.0
    if labels_lower & _FEATURE_LABELS:
        penalty -= 2.0

    # 2. Investigation keywords in title or body -> -1.5
    #    Title match is stronger signal (title is the intent), body match
    #    catches "please investigate why X" in the description.
    has_investigation = any(kw in title_lower for kw in _INVESTIGATION_KW)
    if not has_investigation:
        has_investigation = any(kw in body_lower for kw in _INVESTIGATION_KW)
    if has_investigation:
        penalty -= 1.5

    # 3. Discussion/question labels without "bug" -> -1.0
    #    "help wanted" + "bug" is fine (actionable bug needing help).
    #    "help wanted" alone is often a design discussion.
    is_discussion = bool(labels_lower & _DISCUSSION_LABELS)
    has_bug = "bug" in labels_lower
    if is_discussion and not has_bug:
        penalty -= 1.0

    # 4. Long prose body with no error trace and no code block -> -1.0
    #    Long body + no code = design discussion, not a concrete bug report.
    body_len = len(issue_data.get("body") or "")
    has_error_trace = issue_data.get("has_error_trace", False)
    has_code_block = issue_data.get("has_code_block", False)
    if body_len > 2000 and not has_error_trace and not has_code_block:
        penalty -= 1.0

    return max(-3.5, penalty)
