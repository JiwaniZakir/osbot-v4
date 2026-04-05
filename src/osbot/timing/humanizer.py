"""Human-like timing delays for externally visible actions.

Layer 0 -- no osbot dependencies beyond config constants.
Uses only ``asyncio.sleep`` and ``random``.  All delays are randomized
to prevent recognisable patterns.
"""

from __future__ import annotations

import asyncio
import random


class Humanizer:
    """Add human-like delays to every externally visible action.

    Delay ranges are hardcoded -- they encode behavioural design decisions,
    not tuneable parameters.  Changing them requires understanding the
    anti-detection rationale.
    """

    # Delay ranges in seconds
    PR_DELAY_MIN: int = 900  # 15 min
    PR_DELAY_MAX: int = 2700  # 45 min
    FEEDBACK_DELAY_MIN: int = 1800  # 30 min
    FEEDBACK_DELAY_MAX: int = 14400  # 4 hours
    CLAIM_DELAY_MIN: int = 0  # near-instant
    CLAIM_DELAY_MAX: int = 120  # 2 min
    ENGAGE_DELAY_MIN: int = 300  # 5 min
    ENGAGE_DELAY_MAX: int = 900  # 15 min

    # ------------------------------------------------------------------
    # Async delays (actually sleep)
    # ------------------------------------------------------------------

    async def delay_pr_creation(self) -> None:
        """Wait 15-45 minutes before creating a PR after implementation."""
        seconds = self.jitter((self.PR_DELAY_MIN + self.PR_DELAY_MAX) / 2, variance=0.5)
        seconds = max(self.PR_DELAY_MIN, min(self.PR_DELAY_MAX, seconds))
        await asyncio.sleep(seconds)

    async def delay_feedback_response(self) -> None:
        """Wait 30 min - 4 hours before responding to maintainer feedback."""
        seconds = self.jitter((self.FEEDBACK_DELAY_MIN + self.FEEDBACK_DELAY_MAX) / 2, variance=0.75)
        seconds = max(self.FEEDBACK_DELAY_MIN, min(self.FEEDBACK_DELAY_MAX, seconds))
        await asyncio.sleep(seconds)

    async def delay_claim_comment(self) -> None:
        """Wait 0-2 minutes before posting a claim comment.

        Short delay -- claiming quickly is natural human behaviour.
        """
        seconds = random.uniform(self.CLAIM_DELAY_MIN, self.CLAIM_DELAY_MAX)
        await asyncio.sleep(seconds)

    async def delay_engagement(self) -> None:
        """Wait 5-15 minutes before posting an engagement comment."""
        seconds = self.jitter((self.ENGAGE_DELAY_MIN + self.ENGAGE_DELAY_MAX) / 2, variance=0.3)
        seconds = max(self.ENGAGE_DELAY_MIN, min(self.ENGAGE_DELAY_MAX, seconds))
        await asyncio.sleep(seconds)

    # ------------------------------------------------------------------
    # Jitter utility
    # ------------------------------------------------------------------

    @staticmethod
    def jitter(base_seconds: float, variance: float = 0.3) -> float:
        """Return ``base_seconds`` +/- ``variance`` as a random float.

        Args:
            base_seconds: Centre value in seconds.
            variance: Fractional range (0.3 = +/- 30%).

        Returns:
            A float in ``[base * (1 - variance), base * (1 + variance)]``.
        """
        low = base_seconds * (1 - variance)
        high = base_seconds * (1 + variance)
        return random.uniform(low, high)
