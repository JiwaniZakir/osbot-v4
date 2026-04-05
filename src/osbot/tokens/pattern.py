"""L4: Weekly user pattern model.

Builds and maintains a weekly heatmap of user consumption:
``(day_of_week, hour, 5min_slot) -> avg_user_delta``.

Updated incrementally from each new ``UsageDelta``.  Confidence
starts at 0.0 and grows with sample coverage across the 2016
possible slots in a week (7 days x 24 hours x 12 five-minute slots).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from osbot.config import settings
from osbot.log import get_logger
from osbot.types import MemoryDBProtocol, PatternSlot, UsageDelta

logger = get_logger("tokens.pattern")

_SLOTS_PER_HOUR = 12  # 60 / 5 = 12 five-minute slots
_TOTAL_SLOTS = 7 * 24 * _SLOTS_PER_HOUR  # 2016
_MIN_SAMPLES_FOR_FULL_CONFIDENCE = 4  # per slot


class PatternModel:
    """Weekly heatmap of user token consumption by time-of-week."""

    def __init__(self, memory: MemoryDBProtocol) -> None:
        self._memory = memory
        self._tz = ZoneInfo(settings.timezone)
        # In-memory cache: (day, hour, slot) -> (sum_delta, count)
        self._cache: dict[tuple[int, int, int], tuple[float, int]] = {}
        self._loaded = False

    async def load(self) -> None:
        """Load the pattern from the ``user_pattern`` table into memory."""
        rows: list[dict[str, Any]] = await self._memory.fetchall(
            "SELECT day_of_week, hour, slot, avg_user_delta, sample_count FROM user_pattern"
        )
        self._cache.clear()
        for row in rows:
            key = (int(row["day_of_week"]), int(row["hour"]), int(row["slot"]))
            avg = float(row["avg_user_delta"])
            count = int(row["sample_count"])
            # Reconstruct running sum from average
            self._cache[key] = (avg * count, count)
        self._loaded = True
        logger.info("pattern_loaded", slots=len(self._cache), total_possible=_TOTAL_SLOTS)

    async def record(self, delta: UsageDelta) -> None:
        """Incorporate a new usage delta into the pattern model.

        Maps the delta's period_end timestamp to a (day, hour, slot) key
        and updates the running average for that slot.
        """
        if not self._loaded:
            await self.load()

        ts = datetime.fromisoformat(delta.period_end)
        local = ts.astimezone(self._tz)
        key = (local.weekday(), local.hour, local.minute // 5)

        old_sum, old_count = self._cache.get(key, (0.0, 0))
        new_sum = old_sum + delta.user_delta
        new_count = old_count + 1
        self._cache[key] = (new_sum, new_count)

        avg = new_sum / new_count
        await self._memory.execute(
            """
            INSERT INTO user_pattern (day_of_week, hour, slot, avg_user_delta, sample_count)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (day_of_week, hour, slot)
            DO UPDATE SET avg_user_delta = ?, sample_count = ?
            """,
            (key[0], key[1], key[2], avg, new_count, avg, new_count),
        )

    def predict_user_usage(self, from_time: datetime, hours_ahead: float) -> list[float]:
        """Predict user usage deltas for each 5-minute slot from ``from_time``.

        Args:
            from_time: Start time (UTC or timezone-aware).
            hours_ahead: How many hours to look ahead.

        Returns:
            List of predicted user deltas, one per 5-minute slot.
        """
        slots_count = int(hours_ahead * _SLOTS_PER_HOUR)
        predictions: list[float] = []
        local = from_time.astimezone(self._tz)

        for i in range(slots_count):
            t = local + timedelta(minutes=5 * i)
            key = (t.weekday(), t.hour, t.minute // 5)
            total, count = self._cache.get(key, (0.0, 0))
            avg = total / count if count > 0 else 0.0
            predictions.append(avg)

        return predictions

    @property
    def confidence(self) -> float:
        """Model confidence from 0.0 to 1.0 based on data coverage.

        Full confidence requires ``_MIN_SAMPLES_FOR_FULL_CONFIDENCE``
        observations in at least 80% of weekly slots.
        """
        if not self._cache:
            return 0.0

        covered = sum(
            1 for _, count in self._cache.values()
            if count >= _MIN_SAMPLES_FOR_FULL_CONFIDENCE
        )
        coverage = covered / _TOTAL_SLOTS
        # Scale: 80% coverage = 1.0, linear below
        return min(coverage / 0.8, 1.0)

    @property
    def slot_coverage(self) -> float:
        """Fraction of weekly slots with any data at all (0.0-1.0)."""
        return len(self._cache) / _TOTAL_SLOTS if _TOTAL_SLOTS > 0 else 0.0

    def to_slots(self) -> list[PatternSlot]:
        """Export the current heatmap as a list of ``PatternSlot`` objects."""
        result: list[PatternSlot] = []
        for (day, hour, slot), (total, count) in sorted(self._cache.items()):
            avg = total / count if count > 0 else 0.0
            result.append(PatternSlot(
                day_of_week=day, hour=hour, slot=slot,
                avg_user_delta=avg, sample_count=count,
            ))
        return result
