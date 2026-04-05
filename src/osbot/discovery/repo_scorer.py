"""Repo scorer -- pure arithmetic scoring from signals.

Scores repos 0-10 based on external merge rate, response time,
close-completion rate, star count sweet spot, and CI presence.
Repos scoring below ``settings.repo_score_threshold`` (default 4.0)
are excluded from the active pool.

Zero Claude calls.  Layer 4.
"""

from __future__ import annotations

from typing import Any

from osbot.config import settings
from osbot.log import get_logger
from osbot.types import RepoMeta

logger = get_logger(__name__)


def score_repo(
    meta: RepoMeta,
    signals: dict[str, Any] | None = None,
) -> float:
    """Score a repo 0-10 from its signals.  Pure arithmetic, no I/O.

    Scoring breakdown:
    - Base score: 5.0
    - External merge rate:  >0.40 -> +3.0,  0.20-0.40 -> +1.5,
                            0.05-0.20 -> +0.0,  <0.05 -> -3.0
    - Response time:        <24h -> +1.0,  24-72h -> +0.5,  >168h -> -1.0
    - Close-completion:     >0.60 -> +0.5,  <0.20 -> -0.5
    - Stars sweet spot:     500-5000 -> +0.5 (mid-size, receptive to external PRs)
    - CI present:           +0.5
    - No-AI policy:         -> 0.0 (auto-exclude)

    Args:
        meta: Repository metadata (may include pre-populated signal fields).
        signals: Dict of computed signals from ``compute_signals``.  If None,
                 uses the signal fields already on ``meta``.

    Returns:
        Score clamped to 0.0-10.0.
    """
    # Auto-exclude repos with AI policy
    if meta.has_ai_policy:
        logger.debug("repo_excluded_ai_policy", repo=meta.full_name)
        return 0.0

    sig = signals or {}
    ext_rate = sig.get("external_merge_rate", meta.external_merge_rate)
    avg_hours = sig.get("avg_response_hours", meta.avg_response_hours)
    completion = sig.get("close_completion_rate", meta.close_completion_rate)
    has_ci = sig.get("has_ci", meta.ci_enabled)

    score = 5.0

    # External merge rate adjustment [-3.0, +3.0]
    if ext_rate > 0.40:
        score += 3.0
    elif ext_rate > 0.20:
        score += 1.5
    elif ext_rate >= 0.05:
        score += 0.0
    else:
        # Very low or zero external merge rate -- hostile to outside PRs
        score -= 3.0

    # Response time adjustment [-1.0, +1.0]
    if 0 < avg_hours < 24:
        score += 1.0
    elif avg_hours <= 72:
        score += 0.5
    elif avg_hours > 168:
        score -= 1.0

    # Close-completion rate adjustment [-0.5, +0.5]
    if completion > 0.60:
        score += 0.5
    elif completion < 0.20:
        score -= 0.5

    # Stars sweet spot bonus [0, +0.5]
    if 500 <= meta.stars <= 5000:
        score += 0.5

    # CI bonus [0, +0.5]
    if has_ci:
        score += 0.5

    # Clamp to 0-10
    clamped = max(0.0, min(10.0, score))

    logger.debug(
        "repo_scored",
        repo=meta.full_name,
        score=round(clamped, 2),
        ext_rate=ext_rate,
        avg_hours=avg_hours,
        completion=completion,
        has_ci=has_ci,
    )

    return round(clamped, 2)
