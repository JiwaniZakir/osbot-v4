"""L2: Window decay model.

Tracks the bot's own token consumption as timestamped entries in an
in-memory ledger.  Provides predictions about how much of the 5-hour
rolling window belongs to us at any future time ``t``, and how many
tokens are about to "fall off" the window edge.

Not persisted -- rebuilds naturally from the probe on restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from osbot.config import settings
from osbot.log import get_logger

logger = get_logger("tokens.decay")

_WINDOW_SEC = 5 * 3600  # 5-hour rolling window


@dataclass(slots=True)
class _Entry:
    """A single bot consumption event."""

    ts: datetime
    tokens: int
    model: str


@dataclass
class DecayModel:
    """In-memory ledger of bot token consumption within the 5-hour window.

    The key insight: if the probe says headroom is 5% but 15% of our tokens
    are about to decay off, effective headroom is actually ~20%.
    """

    window_seconds: int = _WINDOW_SEC
    capacity: int = field(default_factory=lambda: settings.estimated_window_capacity)
    _ledger: list[_Entry] = field(default_factory=list)

    # -- public API ----------------------------------------------------------

    def record(self, tokens: int, model: str) -> None:
        """Record a bot consumption event.  Called by the gateway after each call."""
        now = datetime.now(timezone.utc)
        self._ledger.append(_Entry(ts=now, tokens=tokens, model=model))
        self._prune(now)
        logger.debug("decay_recorded", tokens=tokens, model=model, ledger_size=len(self._ledger))

    def bot_tokens_in_window(self, t: datetime | None = None) -> int:
        """Total bot tokens inside the [t-window, t] range."""
        t = t or datetime.now(timezone.utc)
        self._prune(t)
        cutoff = t - timedelta(seconds=self.window_seconds)
        return sum(e.tokens for e in self._ledger if e.ts >= cutoff)

    def bot_utilization_at(self, t: datetime | None = None) -> float:
        """Predicted bot utilization (0.0-1.0) at time ``t``.

        This is ``bot_tokens_in_window(t) / capacity``.
        """
        return min(self.bot_tokens_in_window(t) / self.capacity, 1.0)

    def bot_utilization_now(self) -> float:
        """Current bot utilization (convenience alias)."""
        return self.bot_utilization_at()

    def tokens_decaying_at(self, t: datetime, lookahead_min: int = 30) -> int:
        """Tokens that will fall off the window between now and ``t + lookahead``."""
        now = datetime.now(timezone.utc)
        # Entries that are currently in-window but will be out-of-window at t+lookahead
        future_cutoff = t + timedelta(minutes=lookahead_min) - timedelta(seconds=self.window_seconds)
        current_cutoff = now - timedelta(seconds=self.window_seconds)
        return sum(
            e.tokens
            for e in self._ledger
            if e.ts >= current_cutoff and e.ts < future_cutoff
        )

    def effective_headroom(self, probe_headroom: float) -> float:
        """Adjust probe headroom by accounting for tokens about to decay off.

        If probe says 5% headroom but 15% of capacity is about to decay
        in the next 30 minutes, effective headroom is ~20%.
        """
        now = datetime.now(timezone.utc)
        decaying = self.tokens_decaying_at(now, lookahead_min=30)
        decay_fraction = decaying / self.capacity if self.capacity > 0 else 0.0
        return min(probe_headroom + decay_fraction, 1.0)

    def opus_tokens_in_window(self) -> int:
        """Total Opus tokens in the current window."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self.window_seconds)
        return sum(e.tokens for e in self._ledger if e.ts >= cutoff and e.model == "opus")

    @property
    def entry_count(self) -> int:
        """Number of entries currently in the ledger."""
        return len(self._ledger)

    # -- internals -----------------------------------------------------------

    def _prune(self, t: datetime) -> None:
        """Remove entries older than the window."""
        cutoff = t - timedelta(seconds=self.window_seconds)
        before = len(self._ledger)
        self._ledger = [e for e in self._ledger if e.ts >= cutoff]
        pruned = before - len(self._ledger)
        if pruned > 0:
            logger.debug("decay_pruned", pruned=pruned, remaining=len(self._ledger))
