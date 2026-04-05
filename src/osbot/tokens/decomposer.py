"""L3: Usage decomposer.

Given two consecutive ``UsageSnapshot`` readings and the known bot
consumption between them, decomposes the total usage delta into
bot vs user components.  This is how we learn the user's consumption
pattern without any direct user tracking.

    total_delta - bot_delta = user_delta
"""

from __future__ import annotations

from datetime import datetime, timezone

from osbot.log import get_logger
from osbot.tokens.decay import DecayModel
from osbot.types import MemoryDBProtocol, UsageDelta, UsageSnapshot

logger = get_logger("tokens.decomposer")


class Decomposer:
    """Separates bot vs user consumption between two probe snapshots."""

    def __init__(self, decay: DecayModel, memory: MemoryDBProtocol) -> None:
        self._decay = decay
        self._memory = memory

    async def decompose(
        self,
        prev: UsageSnapshot,
        curr: UsageSnapshot,
        capacity: int,
    ) -> UsageDelta:
        """Compute the bot/user split for a probe interval.

        Args:
            prev: Previous probe snapshot.
            curr: Current probe snapshot.
            capacity: Estimated total token capacity for the 5-hour window.

        Returns:
            A ``UsageDelta`` with total, bot, and user components.
        """
        # Total delta is the change in 5-hour utilization
        total_delta = max(curr.five_hour - prev.five_hour, 0.0)

        # Bot delta: our known consumption as a fraction of capacity
        bot_tokens = self._decay.bot_tokens_in_window()
        prev_ts = datetime.fromisoformat(prev.ts)
        # Tokens we had at previous probe time (approximate)
        prev_bot_tokens = self._decay.bot_tokens_in_window(prev_ts)
        bot_delta_tokens = max(bot_tokens - prev_bot_tokens, 0)
        bot_delta = bot_delta_tokens / capacity if capacity > 0 else 0.0

        # Clamp: bot cannot exceed total
        bot_delta = min(bot_delta, total_delta)

        # User delta is the remainder
        user_delta = max(total_delta - bot_delta, 0.0)

        delta = UsageDelta(
            period_start=prev.ts,
            period_end=curr.ts,
            total_delta=round(total_delta, 6),
            bot_delta=round(bot_delta, 6),
            user_delta=round(user_delta, 6),
        )

        # Persist to memory.db
        await self._persist(delta)

        logger.info(
            "usage_decomposed",
            total=delta.total_delta,
            bot=delta.bot_delta,
            user=delta.user_delta,
        )
        return delta

    async def _persist(self, delta: UsageDelta) -> None:
        """Store the delta in the usage_deltas table."""
        try:
            await self._memory.execute(
                """
                INSERT INTO usage_deltas (period_start, period_end, total_delta, bot_delta, user_delta)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    delta.period_start,
                    delta.period_end,
                    delta.total_delta,
                    delta.bot_delta,
                    delta.user_delta,
                ),
            )
        except Exception as exc:
            logger.warning("decomposer_persist_failed", error=str(exc))
