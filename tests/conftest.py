"""Shared fixtures for osbot v4 test suite.

Provides: in-memory MemoryDB, mock gateway, mock github CLI, sample data.
All tests use pytest-asyncio with asyncio_mode = "auto".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from osbot.state.db import MemoryDB
from osbot.types import (
    AgentResult,
    CLIResult,
    Phase,
    Priority,
    RepoMeta,
    ScoredIssue,
)


# ---------------------------------------------------------------------------
# In-memory MemoryDB with migrations
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path: Path) -> MemoryDB:
    """In-memory MemoryDB with all migrations applied."""
    mem = MemoryDB()
    await mem.connect(tmp_path / "test.db")
    yield mem  # type: ignore[misc]
    await mem.close()


# ---------------------------------------------------------------------------
# Mock ClaudeGatewayProtocol
# ---------------------------------------------------------------------------


@dataclass
class MockGateway:
    """Configurable mock for ClaudeGatewayProtocol.

    Set ``response`` before calling to control what ``invoke`` returns.
    All calls are recorded in ``calls``.
    """

    response: AgentResult = field(
        default_factory=lambda: AgentResult(success=True, text="ok", tokens_used=100)
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def invoke(
        self,
        prompt: str,
        *,
        phase: Phase,
        model: str,
        allowed_tools: list[str],
        cwd: str,
        timeout: float,
        priority: Priority = Priority.DIAGNOSTIC,
        max_turns: int | None = None,
    ) -> AgentResult:
        self.calls.append(
            {
                "prompt": prompt,
                "phase": phase,
                "model": model,
                "allowed_tools": allowed_tools,
                "cwd": cwd,
                "timeout": timeout,
                "priority": priority,
                "max_turns": max_turns,
            }
        )
        return self.response


@pytest.fixture
def mock_gateway() -> MockGateway:
    return MockGateway()


# ---------------------------------------------------------------------------
# Mock GitHubCLIProtocol
# ---------------------------------------------------------------------------


@dataclass
class MockGitHub:
    """Configurable mock for GitHubCLIProtocol.

    Set ``gh_response`` / ``git_response`` before calling.
    """

    gh_response: CLIResult = field(
        default_factory=lambda: CLIResult(returncode=0, stdout="{}", stderr="")
    )
    git_response: CLIResult = field(
        default_factory=lambda: CLIResult(returncode=0, stdout="", stderr="")
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def run_gh(self, args: list[str], cwd: str | None = None) -> CLIResult:
        self.calls.append({"type": "gh", "args": args, "cwd": cwd})
        return self.gh_response

    async def run_git(self, args: list[str], cwd: str | None = None) -> CLIResult:
        self.calls.append({"type": "git", "args": args, "cwd": cwd})
        return self.git_response

    async def graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append({"type": "graphql", "query": query, "variables": variables})
        return {}


@pytest.fixture
def mock_github() -> MockGitHub:
    return MockGitHub()


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_issue() -> ScoredIssue:
    return ScoredIssue(
        repo="owner/repo",
        number=42,
        title="Fix missing import in parser module",
        body="When running `parse_input()`, a `NameError` is raised because `re` is not imported.\n\n```\nTraceback:\n  File parser.py, line 10\nNameError: name 're' is not defined\n```",
        labels=["bug", "good first issue"],
        url="https://github.com/owner/repo/issues/42",
        score=7.5,
        maintainer_confirmed=True,
        has_error_trace=True,
        has_code_block=True,
        requires_assignment=False,
        created_at="2026-03-01T00:00:00Z",
        updated_at="2026-03-20T00:00:00Z",
        comment_count=3,
        reaction_count=2,
    )


@pytest.fixture
def sample_repo() -> RepoMeta:
    return RepoMeta(
        owner="owner",
        name="repo",
        language="Python",
        stars=1500,
        description="An AI framework for building agents",
        topics=["ai", "llm", "python", "agents"],
        has_contributing=True,
        requires_assignment=False,
        has_ai_policy=False,
        ci_enabled=True,
        external_merge_rate=0.35,
        avg_response_hours=12.0,
        close_completion_rate=0.65,
        score=7.0,
    )
