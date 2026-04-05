"""Tests for issue_scorer and repo_scorer -- pure arithmetic scoring.

Covers the v2 four-adjustment formula, benchmark adjustment, and repo scoring.
"""

from __future__ import annotations

from osbot.discovery.issue_scorer import (
    _compute_benchmark_adj,
    _compute_lesson_adj,
    _compute_quality_adj,
    _compute_repo_adj,
    score_issue,
)
from osbot.discovery.repo_scorer import score_repo
from osbot.types import RepoMeta


# ---------------------------------------------------------------------------
# Issue scoring
# ---------------------------------------------------------------------------


async def test_base_score_is_5(sample_repo: RepoMeta) -> None:
    """A bare issue with no signals should score around the base of 5.0."""
    issue_data = {
        "repo": "owner/repo",
        "number": 1,
        "title": "some issue",
        "labels": [],
        "maintainer_confirmed": False,
        "has_error_trace": False,
        "has_code_block": False,
        "comment_count": 0,
        "reaction_count": 0,
    }
    # Use a repo with ~0.10 external merge rate -> repo_adj = 0.0
    repo = RepoMeta(
        owner="owner",
        name="repo",
        language="Python",
        stars=1000,
        external_merge_rate=0.10,
    )
    result = score_issue(issue_data, repo)
    # base(5.0) + repo_adj(0.0) + label_adj(0.0) + quality_adj(0.0) + lesson_adj(0.0) + benchmark_adj(0.0)
    assert result.score == 5.0


async def test_maintainer_confirmed_adds_1_5(sample_repo: RepoMeta) -> None:
    """Maintainer confirmation should add 1.50 via quality_adj."""
    issue_data = {
        "repo": "owner/repo",
        "number": 1,
        "title": "confirmed bug",
        "labels": [],
        "maintainer_confirmed": True,
        "has_error_trace": False,
        "has_code_block": False,
        "comment_count": 0,
        "reaction_count": 0,
    }
    repo = RepoMeta(
        owner="owner",
        name="repo",
        language="Python",
        stars=1000,
        external_merge_rate=0.10,
    )
    result = score_issue(issue_data, repo)
    # base(5.0) + quality_adj(1.50) = 6.50
    assert result.score == 6.5
    assert result.maintainer_confirmed is True


async def test_lesson_penalty_caps_at_minus_3() -> None:
    """Negative lessons should cap at -3.0 total penalty."""
    # 5 negative lessons * -0.75 = -3.75, but capped at -3.0
    lessons = [
        {"sentiment": "negative", "value": f"lesson {i}"} for i in range(5)
    ]
    adj = _compute_lesson_adj(lessons)
    assert adj == -3.0


async def test_benchmark_adj_caps_at_0_5() -> None:
    """Benchmark adjustment should cap at +0.5."""
    issue_data = {
        "repo": "owner/repo",
        "labels": ["bug"],
        "has_error_trace": True,
        "has_code_block": True,
        "body": "x" * 500,  # body_len between 100-2000
    }
    benchmarks = {
        "owner/repo": {
            "common_labels": ["bug"],
            "test_inclusion_rate": 0.9,
            "avg_pr_lines": 30,  # < 60
        }
    }
    adj = _compute_benchmark_adj(issue_data, benchmarks)
    # label match(0.20) + test_rate(0.15) + small fix(0.15) = 0.50, capped at 0.5
    assert adj == 0.5


async def test_quality_adj_code_block_and_error_trace() -> None:
    """Code block (+0.30) and error trace (+0.50) should both contribute."""
    issue_data = {
        "has_error_trace": True,
        "has_code_block": True,
        "labels": [],
        "maintainer_confirmed": False,
        "comment_count": 0,
        "reaction_count": 0,
    }
    adj = _compute_quality_adj(issue_data)
    assert adj == 0.80  # 0.50 + 0.30


# ---------------------------------------------------------------------------
# Repo scoring
# ---------------------------------------------------------------------------


async def test_repo_scorer_excludes_low_merge_rate() -> None:
    """A repo with very low external merge rate should score below threshold."""
    repo = RepoMeta(
        owner="dead",
        name="project",
        language="Python",
        stars=500,
        external_merge_rate=0.01,
        avg_response_hours=200.0,
        close_completion_rate=0.10,
        ci_enabled=False,
    )
    score = score_repo(repo)
    # base(5) + merge(-3) + response(-1) + completion(-0.5) + stars(+0.5) + ci(0) = 1.0
    assert score < 4.0  # Below default threshold


async def test_repo_scorer_boosts_high_merge_rate() -> None:
    """A healthy repo with high merge rate should score well above threshold."""
    repo = RepoMeta(
        owner="healthy",
        name="project",
        language="Python",
        stars=2000,
        external_merge_rate=0.50,
        avg_response_hours=8.0,
        close_completion_rate=0.70,
        ci_enabled=True,
    )
    score = score_repo(repo)
    # base(5) + merge(+3) + response(+1) + completion(+0.5) + stars(+0.5) + ci(+0.5) = 10.0 (clamped)
    assert score >= 8.0


async def test_repo_scorer_excludes_ai_policy() -> None:
    """A repo with has_ai_policy=True should score exactly 0.0."""
    repo = RepoMeta(
        owner="strict",
        name="project",
        language="Python",
        stars=5000,
        has_ai_policy=True,
        external_merge_rate=0.60,
    )
    score = score_repo(repo)
    assert score == 0.0


async def test_repo_adj_blended_with_outcomes() -> None:
    """Repo adj should blend our history (70%) with external rate (30%)."""
    repo = RepoMeta(
        owner="owner",
        name="repo",
        language="Python",
        stars=1000,
        external_merge_rate=0.50,
    )
    # 3 attempts, 2 merged -> our_rate = 0.667
    outcomes = [
        {"repo": "owner/repo", "outcome": "merged"},
        {"repo": "owner/repo", "outcome": "merged"},
        {"repo": "owner/repo", "outcome": "rejected"},
    ]
    adj = _compute_repo_adj(repo, outcomes)
    # blended = 0.7 * 0.667 + 0.3 * 0.5 = 0.617 -> >= 0.50 -> +2.0
    assert adj == 2.0
