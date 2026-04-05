"""Lesson extraction -- event-triggered learning from outcome summaries.

Uses compressed narrative summaries (Pattern 2) for richer lesson synthesis.
When a merge happens, reads recent summaries and extracts a generalizable
lesson stored as a repo_fact. Negative repo_fact lessons are stored on the
3rd+ rejection.

Reflections are generated on EVERY rejection with graduated confidence:
- 1st rejection: confidence=0.3 (could be noise)
- 2nd rejection: confidence=0.6 (emerging pattern)
- 3rd+ rejection: confidence=0.9 (confirmed pattern)

Zero Claude calls for extraction (pattern matching on summaries).
Optional 1 Claude call only for complex patterns the arithmetic can't explain.

Also includes Reflexion (NeurIPS concept): ``generate_reflection()`` assembles
a structured verbal self-feedback on each rejection. Zero Claude calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from osbot.log import get_logger

if TYPE_CHECKING:
    from osbot.types import MemoryDBProtocol

logger = get_logger(__name__)

# -- Issue type classification (for cross-repo reflection matching) ----------

_ISSUE_TYPE_MAP: dict[str, list[str]] = {
    "typo": ["typo", "spelling", "misspelling"],
    "bug_fix": ["bug", "fix", "error", "broken", "crash"],
    "cleanup": ["cleanup", "remove", "deprecated", "unused", "dead code"],
    "docs": ["documentation", "docs", "readme"],
    "missing_import": ["import", "nameerror", "modulenotfounderror"],
    "test": ["test", "coverage", "spec"],
}

# -- Phase-specific reflection templates (what went wrong -> advice) ---------

_PHASE_ADVICE: dict[str, str] = {
    "preflight": "Issue failed preflight checks. Verify the issue is still open and the repo accepts contributions before starting.",
    "assignment": "Assignment was required but not granted. Check if the repo requires explicit assignment before implementing.",
    "workspace": "Workspace setup failed. Ensure the repo can be cloned and a branch can be created.",
    "implement": "Implementation failed. Simplify the approach: fix only the root cause with minimal code changes.",
    "quality_gates": "Quality gates rejected the fix. Keep diff under 50 lines, touch fewer files, and ensure tests and lint pass.",
    "critic": "Critic review rejected the fix. The implementation had scope or correctness issues. Be more conservative in scope.",
    "submit": "PR submission failed. Ensure fork exists and branch can be pushed.",
    "pipeline": "Unexpected pipeline error. Review error details and handle edge cases.",
}

# Patterns that indicate specific lesson types
_FAILURE_PATTERNS: dict[str, str] = {
    "quality gates": "The repo's quality gates are strict. Focus on test coverage and lint compliance.",
    "critic rejected": "The implementation had scope or correctness issues. Keep changes more minimal.",
    "diff too large": "This repo needs very small changes. Keep diffs under 30 lines.",
    "test presence": "This repo requires tests with every change. Always add a test.",
    "assignment": "This repo requires assignment before starting work.",
    "duplicate": "Check for existing PRs more carefully before starting.",
    "preflight": "Pre-check issues — the repo may have special requirements.",
    "scope": "The fix went beyond what was asked. Strictly fix only the reported bug.",
    "style": "Style mismatch — check the repo's lint and formatting conventions.",
}


async def on_merge(repo: str, issue_number: int, db: MemoryDBProtocol) -> str | None:
    """Extract a positive lesson when a PR is merged.

    The iterate phase records outcome="merged" without a summary (it just
    confirms the GitHub state). The actionable "what worked" info lives in
    the SUBMITTED outcome recorded by the pipeline when the PR was created.
    So we look for the most recent submitted summary for this issue.

    Returns the lesson text, or None if no useful lesson could be extracted.
    """
    summaries = await db.get_recent_summaries(repo, limit=5)
    if not summaries:
        return None

    # Prefer the submitted record for this issue (has the full summary).
    # Fall back to any recent summary with content.
    submitted = [s for s in summaries if s.get("outcome") in ("submitted", "merged") and s.get("summary")]
    if not submitted:
        return None

    latest = submitted[0]
    summary_text = latest.get("summary", "")

    # Extract what went right
    lesson = f"Successfully merged. {summary_text}"

    # Store as positive lesson
    await db.set_repo_fact(
        repo=repo,
        key=f"lesson_positive_{issue_number}",
        value=lesson[:500],
        source="merge_event",
        confidence=0.9,
    )

    # Rebuild the fact index since we added a lesson
    if hasattr(db, "rebuild_fact_index"):
        await db.rebuild_fact_index(repo)

    logger.info("lesson_extracted", repo=repo, type="positive", issue=issue_number)
    return lesson


async def on_rejection(repo: str, issue_number: int, reason: str, db: MemoryDBProtocol) -> str | None:
    """Generate a reflection on EVERY rejection with graduated confidence.

    Confidence scales with rejection count for this repo:
    - 1st rejection: confidence=0.3 (could be noise)
    - 2nd rejection: confidence=0.6 (emerging pattern)
    - 3rd+ rejection: confidence=0.9 (confirmed pattern)

    Also stores a negative repo_fact lesson on the 3rd+ rejection.
    Returns the lesson text, or None if no lesson could be extracted.
    """
    # Count rejections for this repo
    all_outcomes = await db.fetchall(
        """
        SELECT outcome, failure_reason, summary
        FROM outcomes
        WHERE repo = ? AND outcome IN ('rejected', 'failed')
        ORDER BY created_at DESC LIMIT 10
        """,
        (repo,),
    )

    rejection_count = len(all_outcomes)

    # Graduated confidence: 1st=0.3, 2nd=0.6, 3rd+=0.9
    _GRADUATED_CONFIDENCE = {1: 0.3, 2: 0.6}
    confidence = _GRADUATED_CONFIDENCE.get(rejection_count, 0.9)

    # Extract lesson from available data
    reasons = [o.get("failure_reason", "") or "" for o in all_outcomes[:5]]
    summaries = [o.get("summary", "") or "" for o in all_outcomes[:5]]

    # Pattern match against known failure types
    combined_text = " ".join(reasons + summaries).lower()
    lesson_parts: list[str] = []

    for pattern, insight in _FAILURE_PATTERNS.items():
        if pattern in combined_text:
            lesson_parts.append(insight)

    if not lesson_parts:
        # Generic lesson from the most common failure reason
        if rejection_count >= 3:
            lesson_parts.append(f"Repeated failures on this repo. Last reasons: {'; '.join(reasons[:3])}")
        else:
            lesson_parts.append(f"Failed on this repo: {reason[:150]}")

    lesson = " ".join(lesson_parts)

    # Store as negative repo_fact lesson on 3rd+ rejection
    if rejection_count >= 3:
        await db.set_repo_fact(
            repo=repo,
            key=f"lesson_negative_{rejection_count}",
            value=lesson[:500],
            source="rejection_pattern",
            confidence=min(0.5 + 0.1 * rejection_count, 0.95),
        )

        if hasattr(db, "rebuild_fact_index"):
            await db.rebuild_fact_index(repo)

    logger.info(
        "lesson_extracted",
        repo=repo,
        type="negative",
        rejection_count=rejection_count,
        confidence=confidence,
        lesson=lesson[:100],
    )
    return lesson


def _classify_issue_type(title: str, labels: list[str]) -> str | None:
    """Classify an issue into a canonical type for cross-repo reflection matching.

    Uses keyword matching on the issue title and labels. Returns the first
    matching type, or ``None`` if no match is found.

    Zero Claude calls -- pure string matching.
    """
    combined = " ".join([title] + labels).lower()
    for issue_type, keywords in _ISSUE_TYPE_MAP.items():
        if any(kw in combined for kw in keywords):
            return issue_type
    return None


async def generate_reflection(
    repo: str,
    issue_number: int,
    title: str,
    labels: list[str],
    failure_phase: str,
    failure_reason: str,
    db: MemoryDBProtocol,
) -> str | None:
    """Generate and store a structured reflection after a pipeline rejection.

    Assembles a verbal self-feedback from structured pipeline data:
    - What phase failed and why
    - What issue type this was
    - Actionable advice for next time

    Zero Claude calls -- pure pattern matching and template assembly.

    Args:
        repo: The repository (owner/name).
        issue_number: The issue number.
        title: The issue title.
        labels: The issue labels.
        failure_phase: The pipeline phase that failed.
        failure_reason: The reason for failure.
        db: Memory DB for storing the reflection.

    Returns:
        The reflection text, or None if storage failed.
    """
    issue_type = _classify_issue_type(title, labels)

    # Build the reflection from phase advice + specific failure reason
    advice = _PHASE_ADVICE.get(failure_phase, f"Failed at {failure_phase}.")

    # Extract the most useful part of failure_reason (strip prefixes like "quality gates:")
    clean_reason = failure_reason
    if ":" in clean_reason:
        # e.g., "critic rejected: scope creep" -> "scope creep"
        parts = clean_reason.split(":", 1)
        if len(parts[1].strip()) > 5:
            clean_reason = parts[1].strip()

    reflection = (
        f"Attempted {issue_type or 'unknown'} fix on {repo}#{issue_number} "
        f"('{title[:60]}') but failed at {failure_phase}: {clean_reason[:100]}. "
        f"{advice}"
    )

    # Compute graduated confidence from rejection count for this repo
    try:
        rejection_count_row = await db.fetchval(
            """
            SELECT COUNT(*) FROM outcomes
            WHERE repo = ? AND outcome IN ('rejected', 'failed')
            """,
            (repo,),
        )
        rejection_count = int(rejection_count_row or 0)
    except Exception:
        rejection_count = 1  # default to low confidence on error

    _GRADUATED_CONFIDENCE = {1: 0.3, 2: 0.6}
    confidence = _GRADUATED_CONFIDENCE.get(rejection_count, 0.9)

    try:
        if hasattr(db, "record_reflection"):
            await db.record_reflection(
                repo=repo,
                issue_number=issue_number,
                failure_phase=failure_phase,
                failure_reason=failure_reason[:500],
                reflection=reflection[:500],
                issue_type=issue_type,
                issue_labels=labels,
                applicable_repos=[repo],
                confidence=confidence,
            )
            logger.info(
                "reflection_generated",
                repo=repo,
                issue=issue_number,
                phase=failure_phase,
                issue_type=issue_type,
                confidence=confidence,
            )
            return reflection
    except Exception as exc:
        logger.warning("reflection_generation_failed", error=str(exc), repo=repo)
    return None


async def on_feedback(
    repo: str,
    maintainer: str,
    feedback_type: str,
    details: str,
    db: MemoryDBProtocol,
) -> None:
    """Update maintainer profile with observed preferences.

    Called after processing maintainer feedback on a PR.
    """
    # Update or insert maintainer profile
    existing = await db.fetchone(
        "SELECT * FROM maintainer_profiles WHERE repo = ? AND username = ?",
        (repo, maintainer),
    )

    now = datetime.now(UTC).isoformat()

    if existing:
        await db.execute(
            "UPDATE maintainer_profiles SET updated_at = ? WHERE repo = ? AND username = ?",
            (now, repo, maintainer),
        )
    else:
        await db.execute(
            """
            INSERT INTO maintainer_profiles (repo, username, avg_days_to_merge,
                                             prefers_small_prs, requests_tests,
                                             last_active, updated_at)
            VALUES (?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (repo, maintainer, now, now),
        )

    logger.debug("maintainer_updated", repo=repo, maintainer=maintainer, feedback=feedback_type)


# ---------------------------------------------------------------------------
# A-Mem style meta-lesson consolidation
# ---------------------------------------------------------------------------

# Minimum number of distinct repos a failure pattern must span to
# become a meta-lesson (prevents overfitting to a single repo).
_MIN_REPOS_FOR_META = 3

# Maps failure_reason keywords to generalizable lesson text.
_META_LESSON_TEMPLATES: dict[str, str] = {
    "scope": "Scope creep is the most common rejection cause. Fix only the exact problem described in the issue.",
    "diff too large": "Large diffs are rejected. Aim for under 30 lines changed.",
    "critic rejected": "Critic rejections indicate quality or scope issues. Keep changes minimal and well-tested.",
    "quality gates": "Quality gates require passing lint and tests. Always run the repo's test suite before committing.",
    "test": "Missing tests cause rejections. Always add a test for your fix if the repo has a test suite.",
    "style": "Style mismatches cause rejections. Follow the repo's existing conventions exactly.",
    "assignment": "Some repos require explicit assignment. Check CONTRIBUTING.md before starting work.",
    "duplicate": "Duplicate PRs damage reputation. Always check for existing PRs addressing the same issue.",
    "timeout": "Complex issues cause timeouts. Choose simpler issues with clear paths to resolution.",
}


async def consolidate_meta_lessons(db: MemoryDBProtocol) -> int:
    """Scan outcomes for cross-repo failure patterns and create meta-lessons.

    Zero Claude calls. Pure SQL aggregation + pattern matching.

    Groups outcomes by failure_reason, finds failure patterns spanning
    3+ repos, and synthesizes a meta-lesson for each. This implements
    A-Mem style memory consolidation where individual experiences are
    generalized into transferable knowledge.

    Returns the number of meta-lessons created or updated.
    """
    if not hasattr(db, "upsert_meta_lesson"):
        logger.debug("consolidate_meta_lessons_skip", reason="db missing upsert_meta_lesson")
        return 0

    # Query outcomes grouped by failure_reason, counting distinct repos
    rows = await db.fetchall(
        """
        SELECT failure_reason,
               COUNT(*) as total_count,
               COUNT(DISTINCT repo) as repo_count,
               GROUP_CONCAT(DISTINCT repo) as repos
        FROM outcomes
        WHERE outcome IN ('rejected', 'failed')
          AND failure_reason IS NOT NULL
          AND failure_reason != ''
        GROUP BY failure_reason
        HAVING COUNT(DISTINCT repo) >= ?
        ORDER BY total_count DESC
        """,
        (_MIN_REPOS_FOR_META,),
    )

    consolidated = 0
    seen_lesson_types: set[str] = set()

    for row in rows:
        failure_reason = (row.get("failure_reason") or "").lower()
        repos_str = row.get("repos", "")
        repo_count = row.get("repo_count", 0)
        total_count = row.get("total_count", 0)
        source_repos = repos_str.split(",") if repos_str else []

        # Match against known failure pattern templates
        lesson_text = ""
        lesson_type = ""
        for pattern_key, template in _META_LESSON_TEMPLATES.items():
            if pattern_key in failure_reason:
                lesson_type = pattern_key.replace(" ", "_")
                lesson_text = template
                break

        if not lesson_text:
            # Generic lesson for patterns we don't have a template for
            lesson_type = f"pattern_{failure_reason[:30].replace(' ', '_')}"
            lesson_text = (
                f"Repeated failure across {repo_count} repos: "
                f"'{row.get('failure_reason', '')[:80]}'. "
                f"Adjust strategy to avoid this pattern."
            )

        # Skip duplicate lesson types (rows are ordered by total_count DESC,
        # so the first occurrence is the most-evidenced one).
        if lesson_type in seen_lesson_types:
            continue
        seen_lesson_types.add(lesson_type)

        # Confidence scales with evidence: more repos and more occurrences = higher
        confidence = min(0.3 + 0.1 * repo_count + 0.02 * total_count, 0.95)

        # Fetch outcome IDs for provenance
        outcome_ids_rows = await db.fetchall(
            """
            SELECT id FROM outcomes
            WHERE failure_reason = ? AND outcome IN ('rejected', 'failed')
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (row.get("failure_reason", ""),),
        )
        outcome_ids = [r["id"] for r in outcome_ids_rows]

        await db.upsert_meta_lesson(
            lesson_type=lesson_type,
            lesson_text=lesson_text,
            source_repos=source_repos,
            confidence=confidence,
            source_outcome_ids=outcome_ids,
        )
        consolidated += 1

        logger.info(
            "meta_lesson_consolidated",
            lesson_type=lesson_type,
            repo_count=repo_count,
            total_occurrences=total_count,
            confidence=round(confidence, 2),
        )

    if consolidated:
        logger.info("meta_lessons_consolidation_done", count=consolidated)
    else:
        logger.debug("meta_lessons_consolidation_done", count=0, reason="no cross-repo patterns found")

    return consolidated


async def generate_real_reflection(
    repo: str,
    issue_number: int,
    title: str,
    labels: list[str],
    failure_phase: str,
    failure_reason: str,
    diff: str,
    gateway: Any,  # ClaudeGatewayProtocol — use Any to avoid import
    db: MemoryDBProtocol,
) -> str | None:
    """Generate a Claude-authored reflection from the actual diff (true Reflexion).

    Unlike generate_reflection() which uses templates, this calls Claude with
    the real diff so it can say specifically what file/function was over-scoped.

    One Claude call (haiku, 60s timeout, ~50 token output).
    Only called on scope and critic rejections where we have the diff.

    Args:
        repo: The repository (owner/name).
        issue_number: The issue number.
        title: The issue title.
        labels: The issue labels.
        failure_phase: The pipeline phase that failed.
        failure_reason: The reason for failure.
        diff: The unified diff that was rejected.
        gateway: Claude gateway (Any to avoid circular imports).
        db: Memory DB for storing the reflection.

    Returns:
        The reflection text, or None on failure.
    """
    from osbot.types import Phase, Priority

    issue_type = _classify_issue_type(title, labels)

    prompt = (
        f"You attempted to fix GitHub issue '{title}' on {repo}#{issue_number} "
        f"and failed at phase '{failure_phase}' with reason: {failure_reason[:200]}.\n\n"
        f"Here is the diff you produced (first 1200 chars):\n"
        f"```diff\n{diff[:1200]}\n```\n\n"
        f"In 2 sentences, what specific files/functions did you touch that were "
        f"UNNECESSARY for this fix, and what should you touch ONLY next time? "
        f"Be concrete with file names."
    )

    try:
        result = await gateway.invoke(
            prompt,
            phase=Phase.CONTRIBUTE,
            model="claude-haiku-4-5-20251001",
            allowed_tools=[],
            cwd="/tmp",
            timeout=60.0,
            priority=Priority.LESSON,
            max_turns=1,
        )

        if not result.success or not result.text.strip():
            raise ValueError(f"gateway call failed or empty: {result.error}")

        reflection_text = result.text.strip()

        # Compute graduated confidence from rejection count for this repo
        try:
            rejection_count_row = await db.fetchval(
                """
                SELECT COUNT(*) FROM outcomes
                WHERE repo = ? AND outcome IN ('rejected', 'failed')
                """,
                (repo,),
            )
            rejection_count = int(rejection_count_row or 0)
        except Exception:
            rejection_count = 1

        _GRADUATED_CONFIDENCE = {1: 0.3, 2: 0.6}
        confidence = _GRADUATED_CONFIDENCE.get(rejection_count, 0.9)

        if hasattr(db, "record_reflection"):
            await db.record_reflection(
                repo=repo,
                issue_number=issue_number,
                failure_phase=failure_phase,
                failure_reason=failure_reason[:500],
                reflection=reflection_text[:500],
                issue_type=issue_type,
                issue_labels=labels,
                confidence=confidence,
            )

        logger.info(
            "real_reflection_generated",
            repo=repo,
            issue=issue_number,
            phase=failure_phase,
        )
        return reflection_text

    except Exception as exc:
        logger.debug(
            "real_reflection_fallback",
            repo=repo,
            issue=issue_number,
            error=str(exc),
        )
        # Fall back to template-based reflection
        return await generate_reflection(
            repo=repo,
            issue_number=issue_number,
            title=title,
            labels=labels,
            failure_phase=failure_phase,
            failure_reason=failure_reason,
            db=db,
        )
