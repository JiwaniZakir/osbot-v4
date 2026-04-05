"""Tests for self-referencing guards -- bot must not process its own activity.

Covers:
- Monitor filtering bot's own comments and reviews from PR feedback
- Engage phase skipping issues with existing claims
- Preflight duplicate detection case-insensitivity
- Claim detection ignoring the bot's own claim comments
- Notify phase filtering bot's own comments from thread context
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

from osbot.intel.duplicates import check_claimed_in_comments
from osbot.iteration.monitor import _poll_single_pr
from osbot.orchestrator.engage import _ensure_engaged_table, _pick_engagement_candidates
from osbot.orchestrator.notify import _get_thread_context
from osbot.pipeline.preflight import _check_duplicate_pr
from osbot.state.bot_state import BotState
from osbot.state.db import MemoryDB
from osbot.types import CLIResult, OpenPR, ScoredIssue

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BOT_USERNAME = "JiwaniZakir"


def _make_open_pr(
    repo: str = "owner/repo",
    issue_number: int = 42,
    pr_number: int = 100,
) -> OpenPR:
    return OpenPR(
        repo=repo,
        issue_number=issue_number,
        pr_number=pr_number,
        url=f"https://github.com/{repo}/pull/{pr_number}",
        branch="fix-42",
        submitted_at="2026-03-01T00:00:00Z",
        last_checked_at="2026-03-01T00:00:00Z",
    )


def _make_scored_issue(
    repo: str = "owner/repo",
    number: int = 42,
    score: float = 7.5,
) -> ScoredIssue:
    return ScoredIssue(
        repo=repo,
        number=number,
        title="Fix bug",
        body="There is a bug.",
        labels=["bug"],
        url=f"https://github.com/{repo}/issues/{number}",
        score=score,
    )


class FakeGitHub:
    """Minimal mock implementing GitHubCLIProtocol for these tests."""

    def __init__(self) -> None:
        self.graphql_response: dict = {}
        self.gh_response: CLIResult = CLIResult(returncode=0, stdout="{}", stderr="")
        self.calls: list[dict] = []

    async def run_gh(self, args: list[str], cwd: str | None = None) -> CLIResult:
        self.calls.append({"type": "gh", "args": args, "cwd": cwd})
        return self.gh_response

    async def run_git(self, args: list[str], cwd: str | None = None) -> CLIResult:
        self.calls.append({"type": "git", "args": args, "cwd": cwd})
        return CLIResult(returncode=0, stdout="", stderr="")

    async def graphql(self, query: str, variables: dict | None = None) -> dict:
        self.calls.append({"type": "graphql", "query": query, "variables": variables})
        return self.graphql_response


# ---------------------------------------------------------------------------
# 1. Monitor filters bot's own comments
# ---------------------------------------------------------------------------


@patch("osbot.iteration.monitor.settings")
async def test_monitor_filters_bot_comments(mock_settings: object) -> None:
    """Bot's own comments must not appear in PRUpdate.new_comments."""
    mock_settings.github_username = BOT_USERNAME  # type: ignore[attr-defined]

    github = FakeGitHub()
    github.graphql_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "state": "OPEN",
                    "merged": False,
                    "mergeable": "MERGEABLE",
                    "comments": {
                        "nodes": [
                            {
                                "author": {"login": BOT_USERNAME},
                                "authorAssociation": "NONE",
                                "body": "I submitted this fix.",
                                "createdAt": "2026-03-20T12:00:00Z",
                            },
                            {
                                "author": {"login": "maintainer"},
                                "authorAssociation": "MEMBER",
                                "body": "Please add a test.",
                                "createdAt": "2026-03-20T13:00:00Z",
                            },
                        ],
                    },
                    "reviews": {"nodes": []},
                    "commits": {"nodes": []},
                },
            },
        },
    }

    pr = _make_open_pr()
    update = await _poll_single_pr(pr, github)

    assert update is not None
    # Only the maintainer's comment should pass through
    assert len(update.new_comments) == 1
    assert update.new_comments[0]["author"]["login"] == "maintainer"


# ---------------------------------------------------------------------------
# 2. Monitor filters bot's own reviews
# ---------------------------------------------------------------------------


@patch("osbot.iteration.monitor.settings")
async def test_monitor_filters_bot_reviews(mock_settings: object) -> None:
    """Bot's own reviews must not appear in PRUpdate.new_reviews."""
    mock_settings.github_username = BOT_USERNAME  # type: ignore[attr-defined]

    github = FakeGitHub()
    github.graphql_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "state": "OPEN",
                    "merged": False,
                    "mergeable": "MERGEABLE",
                    "comments": {"nodes": []},
                    "reviews": {
                        "nodes": [
                            {
                                "author": {"login": BOT_USERNAME},
                                "authorAssociation": "NONE",
                                "body": "Self-review check.",
                                "state": "COMMENTED",
                                "createdAt": "2026-03-20T12:00:00Z",
                                "comments": {"nodes": []},
                            },
                            {
                                "author": {"login": "reviewer"},
                                "authorAssociation": "MEMBER",
                                "body": "Looks like there's a type error.",
                                "state": "CHANGES_REQUESTED",
                                "createdAt": "2026-03-20T14:00:00Z",
                                "comments": {"nodes": []},
                            },
                        ],
                    },
                    "commits": {"nodes": []},
                },
            },
        },
    }

    pr = _make_open_pr()
    update = await _poll_single_pr(pr, github)

    assert update is not None
    assert len(update.new_reviews) == 1
    assert update.new_reviews[0]["author"]["login"] == "reviewer"


# ---------------------------------------------------------------------------
# 3. Monitor preserves all comments when github_username is empty
# ---------------------------------------------------------------------------


@patch("osbot.iteration.monitor.settings")
async def test_monitor_preserves_comments_when_no_username(mock_settings: object) -> None:
    """When github_username is empty, no filtering by author should occur.

    All comments (including the bot's, if login matches nothing) pass through.
    """
    mock_settings.github_username = ""  # type: ignore[attr-defined]

    github = FakeGitHub()
    github.graphql_response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "state": "OPEN",
                    "merged": False,
                    "mergeable": "MERGEABLE",
                    "comments": {
                        "nodes": [
                            {
                                "author": {"login": "alice"},
                                "authorAssociation": "NONE",
                                "body": "First comment.",
                                "createdAt": "2026-03-20T12:00:00Z",
                            },
                            {
                                "author": {"login": "bob"},
                                "authorAssociation": "MEMBER",
                                "body": "Second comment.",
                                "createdAt": "2026-03-20T13:00:00Z",
                            },
                        ],
                    },
                    "reviews": {"nodes": []},
                    "commits": {"nodes": []},
                },
            },
        },
    }

    pr = _make_open_pr()
    update = await _poll_single_pr(pr, github)

    assert update is not None
    # Both comments should appear -- no author is filtered when username is empty
    assert len(update.new_comments) == 2


# ---------------------------------------------------------------------------
# 4. Engage skips issues with a prior claim in repo_facts
# ---------------------------------------------------------------------------


async def test_engage_skips_claimed_issues(tmp_path: Path) -> None:
    """Issues with a claim_ts_* fact in the DB should be excluded from engagement."""
    db = MemoryDB()
    await db.connect(tmp_path / "test.db")

    try:
        issue = _make_scored_issue(repo="owner/repo", number=99, score=8.0)

        # Build a BotState with the issue in the queue
        state = BotState(path=tmp_path / "state.json")
        await state.enqueue([issue])

        # Ensure the engaged_issues table exists (normally done by run_engage_phase)
        await _ensure_engaged_table(db)

        # Record a claim fact for this issue
        await db.set_repo_fact("owner/repo", "claim_ts_99", "2026-03-20T00:00:00Z", "preflight")

        # Pick candidates -- should skip issue #99 because of the claim fact
        candidates = await _pick_engagement_candidates(state, db)

        assert all(c.number != 99 for c in candidates), (
            "Issue #99 should be skipped because it has a claim_ts_99 repo fact"
        )
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 5. Preflight duplicate detection is case-insensitive
# ---------------------------------------------------------------------------


@patch("osbot.pipeline.preflight.settings")
async def test_preflight_duplicate_case_insensitive(mock_settings: object) -> None:
    """Author comparison in _check_duplicate_pr must be case-insensitive.

    The bot's username might be 'JiwaniZakir' but GitHub returns 'jiwanizakir'.
    """
    mock_settings.github_username = "JiwaniZakir"  # type: ignore[attr-defined]

    import json

    issue = _make_scored_issue(repo="owner/repo", number=42)

    github = FakeGitHub()
    # Simulate GitHub returning our PR with a lowercase author login
    pr_data = [
        {
            "number": 200,
            "title": "Fix #42",
            "body": "Closes #42",
            "author": {"login": "jiwanizakir"},  # lowercase
        },
    ]
    github.gh_response = CLIResult(
        returncode=0,
        stdout=json.dumps(pr_data),
        stderr="",
    )

    ok, reason = await _check_duplicate_pr(issue, github)

    assert not ok, "Case-insensitive match should detect our own PR as a duplicate"
    assert "duplicate" in reason.lower() or "our open" in reason.lower()


# ---------------------------------------------------------------------------
# 6. Claim detection ignores bot's own claims
# ---------------------------------------------------------------------------


def test_claim_detection_ignores_own_claims() -> None:
    """check_claimed_in_comments must skip the bot's own claim comments."""
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    comments = [
        {
            "author": {"login": BOT_USERNAME},
            "body": "I'm working on this issue.",
            "createdAt": now_str,
        },
        {
            "author": {"login": "helpful-user"},
            "body": "Thanks for looking into this!",
            "createdAt": now_str,
        },
    ]

    claimed, claimer = check_claimed_in_comments(comments, bot_username=BOT_USERNAME)

    assert not claimed, "Bot's own claim comment must not block itself"
    assert claimer == ""


def test_claim_detection_flags_other_claims() -> None:
    """check_claimed_in_comments must detect claims from other users."""
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    comments = [
        {
            "author": {"login": "other-dev"},
            "body": "I'm working on this, PR incoming!",
            "createdAt": now_str,
        },
    ]

    claimed, claimer = check_claimed_in_comments(comments, bot_username=BOT_USERNAME)

    assert claimed, "Another user's claim should be detected"
    assert claimer == "other-dev"


def test_claim_detection_case_insensitive_bot_username() -> None:
    """Bot username comparison in claim detection must be case-insensitive."""
    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    comments = [
        {
            "author": {"login": "jiwanizakir"},  # lowercase
            "body": "I'll submit a PR for this.",
            "createdAt": now_str,
        },
    ]

    # Pass the username in mixed case
    claimed, claimer = check_claimed_in_comments(comments, bot_username="JiwaniZakir")

    assert not claimed, "Case-insensitive bot username match must filter own claims"


# ---------------------------------------------------------------------------
# 7. Notify phase filters bot's own comments from thread context
# ---------------------------------------------------------------------------


@patch("osbot.orchestrator.notify.settings")
async def test_notify_filters_own_comments(mock_settings: object) -> None:
    """Bot's own comments must not appear in the recent_comments of thread context."""
    mock_settings.github_username = BOT_USERNAME  # type: ignore[attr-defined]

    import json

    github = FakeGitHub()

    # Set up the notification object
    notification = {
        "id": "thread-123",
        "subject": {
            "url": "https://api.github.com/repos/owner/repo/issues/10",
            "type": "Issue",
            "title": "Bug report",
            "latest_comment_url": "",
        },
        "repository": {"full_name": "owner/repo"},
    }

    # First call: fetching the subject body
    subject_body_response = CLIResult(
        returncode=0,
        stdout=json.dumps({"body": "Some issue body", "number": 10}),
        stderr="",
    )
    # Second call: fetching comments
    comments_response = CLIResult(
        returncode=0,
        stdout=json.dumps([
            {
                "user": {"login": BOT_USERNAME},
                "body": "I submitted a fix for this.",
            },
            {
                "user": {"login": "maintainer-alice"},
                "body": "Can you add a test for the edge case?",
            },
            {
                "user": {"login": "contributor-bob"},
                "body": "I also hit this bug.",
            },
        ]),
        stderr="",
    )

    # Return different responses for sequential calls
    call_count = 0

    async def _sequenced_run_gh(args: list[str], cwd: str | None = None) -> CLIResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return subject_body_response
        return comments_response

    github.run_gh = _sequenced_run_gh  # type: ignore[assignment]

    context = await _get_thread_context(github, notification)

    # Bot's comment must be filtered out
    comment_authors = [c["author"] for c in context["recent_comments"]]
    assert BOT_USERNAME not in comment_authors, (
        f"Bot's own comment should be filtered from thread context, got authors: {comment_authors}"
    )
    # Other comments should remain
    assert "maintainer-alice" in comment_authors
    assert "contributor-bob" in comment_authors
