"""Tests for quality-gate behaviour on distinct CLI sentinel values.

A1: ``gateway/github.py`` returns ``CLI_RC_NOT_FOUND`` when the binary is
missing, ``CLI_RC_TIMEOUT`` on timeout, and ``CLI_RC_EXC`` on unexpected
exceptions. Only the not-found case may cause the lint/test gate to no-op;
the other two must fail the gate so a crashed/hung linter does not let
broken code through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from osbot.pipeline.quality import _run_lint, _run_tests
from osbot.types import CLI_RC_EXC, CLI_RC_NOT_FOUND, CLI_RC_TIMEOUT, CLIResult


@dataclass
class ScriptedGitHub:
    """Returns a scripted CLIResult for each ``run_cmd`` call, in order."""

    responses: list[CLIResult] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    async def run_cmd(self, cmd: list[str], cwd: str | None = None, timeout: float = 60.0) -> CLIResult:
        self.calls.append(cmd)
        return self.responses.pop(0)

    async def run_gh(self, args: list[str], cwd: str | None = None) -> CLIResult:
        return CLIResult(returncode=0, stdout="", stderr="")

    async def run_git(self, args: list[str], cwd: str | None = None) -> CLIResult:
        return CLIResult(returncode=0, stdout="", stderr="")

    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        return {}


# ---------------------------------------------------------------------------
# _run_lint
# ---------------------------------------------------------------------------


async def test_lint_no_binary_available_passes() -> None:
    """Both linters missing -> gate no-ops (returns True)."""
    gh = ScriptedGitHub(
        responses=[
            CLIResult(returncode=CLI_RC_NOT_FOUND, stdout="", stderr="binary not found: ruff"),
            CLIResult(returncode=CLI_RC_NOT_FOUND, stdout="", stderr="binary not found: flake8"),
        ]
    )
    assert await _run_lint("/ws", gh) is True
    assert len(gh.calls) == 2  # tried both


async def test_lint_ruff_success_passes() -> None:
    gh = ScriptedGitHub(responses=[CLIResult(returncode=0, stdout="", stderr="")])
    assert await _run_lint("/ws", gh) is True
    assert len(gh.calls) == 1  # short-circuits on first available linter


async def test_lint_ruff_real_failure_fails() -> None:
    """Real non-zero exit from the linter = lint failed."""
    gh = ScriptedGitHub(responses=[CLIResult(returncode=1, stdout="E501", stderr="")])
    assert await _run_lint("/ws", gh) is False


async def test_lint_ruff_timeout_fails_closed() -> None:
    """Timeout must NOT be treated as 'no linter available'."""
    gh = ScriptedGitHub(responses=[CLIResult(returncode=CLI_RC_TIMEOUT, stdout="", stderr="timeout")])
    assert await _run_lint("/ws", gh) is False


async def test_lint_ruff_exception_fails_closed() -> None:
    """Unexpected exception must NOT be treated as 'no linter available'."""
    gh = ScriptedGitHub(responses=[CLIResult(returncode=CLI_RC_EXC, stdout="", stderr="boom")])
    assert await _run_lint("/ws", gh) is False


async def test_lint_ruff_missing_flake8_runs() -> None:
    """If ruff is missing we should try flake8 next."""
    gh = ScriptedGitHub(
        responses=[
            CLIResult(returncode=CLI_RC_NOT_FOUND, stdout="", stderr=""),
            CLIResult(returncode=0, stdout="", stderr=""),
        ]
    )
    assert await _run_lint("/ws", gh) is True
    assert len(gh.calls) == 2


# ---------------------------------------------------------------------------
# _run_tests
# ---------------------------------------------------------------------------


async def test_tests_no_pytest_available_passes() -> None:
    gh = ScriptedGitHub(responses=[CLIResult(returncode=CLI_RC_NOT_FOUND, stdout="", stderr="")])
    assert await _run_tests("/ws", gh) is True


async def test_tests_success_passes() -> None:
    gh = ScriptedGitHub(responses=[CLIResult(returncode=0, stdout="", stderr="")])
    assert await _run_tests("/ws", gh) is True


async def test_tests_real_failure_fails() -> None:
    gh = ScriptedGitHub(responses=[CLIResult(returncode=1, stdout="F", stderr="")])
    assert await _run_tests("/ws", gh) is False


async def test_tests_timeout_fails_closed() -> None:
    gh = ScriptedGitHub(responses=[CLIResult(returncode=CLI_RC_TIMEOUT, stdout="", stderr="timeout")])
    assert await _run_tests("/ws", gh) is False


async def test_tests_exception_fails_closed() -> None:
    gh = ScriptedGitHub(responses=[CLIResult(returncode=CLI_RC_EXC, stdout="", stderr="boom")])
    assert await _run_tests("/ws", gh) is False
