"""Tests for the token decay model (L2).

Covers recording, pruning, utilization calculation, and effective headroom.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from osbot.tokens.decay import DecayModel, _Entry


async def test_record_increases_utilization() -> None:
    """Recording tokens should increase bot utilization."""
    dm = DecayModel(capacity=1_000_000)
    assert dm.bot_utilization_at() == 0.0

    dm.record(100_000, "sonnet")
    util = dm.bot_utilization_at()
    assert util > 0.0
    assert abs(util - 0.10) < 0.01  # 100k / 1M = 0.10


async def test_old_entries_pruned() -> None:
    """Entries older than the window should be pruned."""
    dm = DecayModel(window_seconds=5 * 3600, capacity=1_000_000)

    # Manually insert an old entry (6 hours ago)
    old_ts = datetime.now(UTC) - timedelta(hours=6)
    dm._ledger.append(_Entry(ts=old_ts, tokens=500_000, model="sonnet"))

    # After pruning (triggered by bot_tokens_in_window), old entry should be gone
    tokens = dm.bot_tokens_in_window()
    assert tokens == 0
    assert dm.entry_count == 0


async def test_recent_entries_kept() -> None:
    """Entries within the window should be kept."""
    dm = DecayModel(window_seconds=5 * 3600, capacity=1_000_000)
    dm.record(200_000, "sonnet")
    dm.record(100_000, "opus")

    tokens = dm.bot_tokens_in_window()
    assert tokens == 300_000
    assert dm.entry_count == 2


async def test_effective_headroom() -> None:
    """Effective headroom should account for tokens about to decay off."""
    dm = DecayModel(window_seconds=5 * 3600, capacity=1_000_000)

    # Insert entries near the edge of the window (4.5 hours ago)
    old_ts = datetime.now(UTC) - timedelta(hours=4, minutes=45)
    dm._ledger.append(_Entry(ts=old_ts, tokens=150_000, model="sonnet"))

    # Probe says 5% headroom, but ~15% of our tokens are about to decay
    effective = dm.effective_headroom(0.05)
    # Should be > 0.05 because decaying tokens boost headroom
    assert effective >= 0.05


async def test_opus_tokens_tracked() -> None:
    """opus_tokens_in_window should only count opus model entries."""
    dm = DecayModel(capacity=1_000_000)
    dm.record(100_000, "opus")
    dm.record(200_000, "sonnet")

    assert dm.opus_tokens_in_window() == 100_000


async def test_utilization_capped_at_1() -> None:
    """Bot utilization should never exceed 1.0."""
    dm = DecayModel(capacity=100)  # tiny capacity
    dm.record(500, "sonnet")  # way over capacity

    assert dm.bot_utilization_at() == 1.0


async def test_entry_count() -> None:
    """entry_count should reflect current ledger size."""
    dm = DecayModel(capacity=1_000_000)
    assert dm.entry_count == 0
    dm.record(1000, "sonnet")
    assert dm.entry_count == 1
    dm.record(2000, "opus")
    assert dm.entry_count == 2
