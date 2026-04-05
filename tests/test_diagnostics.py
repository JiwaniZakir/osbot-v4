"""Tests for fast_diagnostic -- per-cycle pattern detection.

Covers loop detection, timeout escalation, dead cycle detection,
TOS halt detection, and clean traces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from osbot.learning.diagnostics import fast_diagnostic
from osbot.types import Trace


def _make_trace(
    repo: str = "owner/repo",
    issue_number: int = 1,
    phase: str = "contribute",
    outcome: str = "error",
    failure_reason: str | None = None,
) -> Trace:
    """Helper to build a Trace with defaults."""
    return Trace(
        ts="2026-03-25T00:00:00Z",
        repo=repo,
        issue_number=issue_number,
        phase=phase,
        outcome=outcome,
        failure_reason=failure_reason,
    )


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------


@patch("osbot.comms.webhook.send_alert", new_callable=AsyncMock, return_value=True)
async def test_loop_detection_3_same_errors(mock_alert: AsyncMock) -> None:
    """3+ traces with the same repo and error should trigger a ban correction."""
    traces = [
        _make_trace(outcome="error", failure_reason="lint_failed"),
        _make_trace(outcome="error", failure_reason="lint_failed"),
        _make_trace(outcome="error", failure_reason="lint_failed"),
    ]
    corrections = await fast_diagnostic(traces)
    ban_corrections = [c for c in corrections if c.type == "ban_repo"]
    assert len(ban_corrections) >= 1
    assert ban_corrections[0].repo == "owner/repo"
    assert ban_corrections[0].days == 7
    assert "loop" in ban_corrections[0].reason


# ---------------------------------------------------------------------------
# Timeout escalation
# ---------------------------------------------------------------------------


@patch("osbot.comms.webhook.send_alert", new_callable=AsyncMock, return_value=True)
async def test_timeout_escalation(mock_alert: AsyncMock) -> None:
    """4+ timeouts on the same repo should trigger a 14-day ban correction."""
    traces = [
        _make_trace(outcome="timeout", failure_reason="planning_timeout"),
        _make_trace(outcome="timeout", failure_reason="planning_timeout"),
        _make_trace(outcome="timeout", failure_reason="planning_timeout"),
        _make_trace(outcome="timeout", failure_reason="planning_timeout"),
    ]
    corrections = await fast_diagnostic(traces)
    ban_corrections = [c for c in corrections if c.type == "ban_repo" and c.days == 14]
    assert len(ban_corrections) >= 1
    assert "timeout" in ban_corrections[0].reason


@patch("osbot.comms.webhook.send_alert", new_callable=AsyncMock, return_value=True)
async def test_timeout_warning_at_2(mock_alert: AsyncMock) -> None:
    """2 timeouts should produce a score adjustment correction, not a ban."""
    traces = [
        _make_trace(outcome="timeout"),
        _make_trace(outcome="timeout"),
    ]
    corrections = await fast_diagnostic(traces)
    score_corrections = [c for c in corrections if c.type == "adjust_score"]
    assert len(score_corrections) >= 1
    # Should NOT have a ban
    ban_corrections = [c for c in corrections if c.type == "ban_repo"]
    assert len(ban_corrections) == 0


# ---------------------------------------------------------------------------
# Dead cycle detection
# ---------------------------------------------------------------------------


@patch("osbot.comms.webhook.send_alert", new_callable=AsyncMock, return_value=True)
async def test_dead_cycle_detection(mock_alert: AsyncMock) -> None:
    """15+ traces with 0 submissions should trigger a force_discovery correction."""
    traces = [_make_trace(outcome="error", failure_reason=f"error_{i}") for i in range(16)]
    corrections = await fast_diagnostic(traces)
    force_corrections = [c for c in corrections if c.type == "force_discovery"]
    assert len(force_corrections) >= 1
    assert "dead" in force_corrections[0].reason.lower()


# ---------------------------------------------------------------------------
# TOS halt detection
# ---------------------------------------------------------------------------


@patch("osbot.comms.webhook.send_alert", new_callable=AsyncMock, return_value=True)
async def test_tos_halt_detection(mock_alert: AsyncMock) -> None:
    """A trace with 'tos' in the failure reason should trigger a halt."""
    traces = [
        _make_trace(outcome="error", failure_reason="tos dialog detected, must accept terms"),
    ]
    corrections = await fast_diagnostic(traces)
    halt_corrections = [c for c in corrections if c.type == "halt"]
    assert len(halt_corrections) >= 1
    assert halt_corrections[0].severity == "critical"


@patch("osbot.comms.webhook.send_alert", new_callable=AsyncMock, return_value=True)
async def test_auth_error_halt_detection(mock_alert: AsyncMock) -> None:
    """A trace with 'auth error' should trigger a halt."""
    traces = [
        _make_trace(outcome="error", failure_reason="auth error: not logged in"),
    ]
    corrections = await fast_diagnostic(traces)
    halt_corrections = [c for c in corrections if c.type == "halt"]
    assert len(halt_corrections) >= 1


# ---------------------------------------------------------------------------
# Clean traces
# ---------------------------------------------------------------------------


async def test_no_corrections_on_clean_traces() -> None:
    """Traces with successful outcomes should produce no corrections."""
    traces = [
        _make_trace(outcome="success"),
        _make_trace(outcome="merged"),
        _make_trace(outcome="submitted"),
    ]
    corrections = await fast_diagnostic(traces)
    assert corrections == []


async def test_empty_traces_no_corrections() -> None:
    """Empty trace list should produce no corrections."""
    corrections = await fast_diagnostic([])
    assert corrections == []
