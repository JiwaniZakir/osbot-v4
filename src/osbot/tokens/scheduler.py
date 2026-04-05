"""L4: Predictive scheduler.

Uses the pattern model + decay model + current probe to generate a
worker plan for the next N hours.  Simulates forward: predicted user
usage + our expected consumption + decay of old tokens.

Cold start fallback: 2 workers during 9am-6pm weekdays, 4 off-hours,
when confidence < 0.3.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from osbot.config import settings
from osbot.log import get_logger
from osbot.tokens.decay import DecayModel
from osbot.tokens.pattern import PatternModel
from osbot.types import UsageSnapshot, WorkerPlan

logger = get_logger("tokens.scheduler")

# Average tokens per contribution worker per 5-minute slot (rough estimate).
# A contribution cycle uses ~150k tokens across 3 calls over ~15 min,
# so per 5-min slot per worker: ~50k tokens.
_TOKENS_PER_WORKER_PER_SLOT = 50_000


class Scheduler:
    """Predictive worker scheduler based on pattern model and decay model."""

    def __init__(self, pattern: PatternModel, decay: DecayModel) -> None:
        self._pattern = pattern
        self._decay = decay
        self._tz = ZoneInfo(settings.timezone)

    async def plan(
        self,
        snapshot: UsageSnapshot,
        hours_ahead: float | None = None,
    ) -> WorkerPlan:
        """Generate a worker plan for the next ``hours_ahead`` hours.

        Args:
            snapshot: Latest probe reading.
            hours_ahead: Planning horizon.  Defaults to ``settings.plan_horizon_hours``.

        Returns:
            A ``WorkerPlan`` specifying how many workers to run.
        """
        horizon = hours_ahead or settings.plan_horizon_hours
        now = datetime.now(timezone.utc)
        confidence = self._pattern.confidence

        # Cold start: not enough pattern data
        if confidence < 0.3:
            workers = self._cold_start_workers(now)
            return WorkerPlan(
                workers=workers,
                reason=f"cold_start (confidence={confidence:.2f})",
                confidence=confidence,
            )

        # Predict user usage over the horizon
        user_predictions = self._pattern.predict_user_usage(now, horizon)
        predicted_user_total = sum(user_predictions)

        # Current headroom (adjusted for decay)
        current_headroom = settings.five_hour_ceiling - snapshot.five_hour
        effective = self._decay.effective_headroom(current_headroom)

        # How many tokens can we spend?  Effective headroom * capacity.
        budget_tokens = effective * settings.estimated_window_capacity

        # Subtract predicted user consumption from our budget
        predicted_user_tokens = predicted_user_total * settings.estimated_window_capacity
        available_tokens = max(budget_tokens - predicted_user_tokens, 0)

        # How many workers can we sustain over the horizon?
        slots_in_horizon = int(horizon * 12)  # 12 five-minute slots per hour
        if slots_in_horizon > 0 and _TOKENS_PER_WORKER_PER_SLOT > 0:
            max_workers_float = available_tokens / (slots_in_horizon * _TOKENS_PER_WORKER_PER_SLOT)
        else:
            max_workers_float = 1.0

        # Blend with cold start based on confidence
        cold = self._cold_start_workers(now)
        blended = (confidence * max_workers_float) + ((1.0 - confidence) * cold)

        # Clamp to [1, max_workers]
        workers = max(1, min(int(round(blended)), settings.max_workers))

        # Headroom at end of horizon (rough estimate)
        our_usage = workers * slots_in_horizon * _TOKENS_PER_WORKER_PER_SLOT
        total_usage = (our_usage + predicted_user_tokens) / settings.estimated_window_capacity
        headroom_at_horizon = max(settings.five_hour_ceiling - snapshot.five_hour - total_usage, 0.0)

        return WorkerPlan(
            workers=workers,
            reason=f"predicted (conf={confidence:.2f}, eff_headroom={effective:.2f})",
            confidence=confidence,
            predicted_user_usage=round(predicted_user_total, 4),
            headroom_at_horizon=round(headroom_at_horizon, 4),
        )

    async def plan_without_probe(self) -> WorkerPlan:
        """Generate a worker plan when probe data is unavailable.

        Uses the decay model (our own consumption) + time-of-day scheduling.
        If pattern data exists, uses it; otherwise falls back to cold start.

        This is the reliable fallback when the OAuth usage endpoint is
        rate-limited or unreachable.
        """
        now = datetime.now(timezone.utc)
        confidence = self._pattern.confidence

        # Use bot's own consumption as a self-check
        bot_util = self._decay.bot_utilization_now()

        if confidence >= 0.3:
            # Have pattern data — use predicted user usage to scale
            user_predictions = self._pattern.predict_user_usage(now, 1.0)
            predicted_user = sum(user_predictions)

            # If we're consuming a lot + user is predicted active, throttle
            if bot_util > 0.3 and predicted_user > 0.01:
                workers = max(1, settings.cold_start_workers_peak - 1)
                reason = f"schedule_only: high bot util ({bot_util:.2f}) + user predicted ({predicted_user:.3f})"
            elif bot_util > 0.5:
                workers = settings.cold_start_workers_peak
                reason = f"schedule_only: high bot util ({bot_util:.2f}), throttling"
            else:
                workers = self._cold_start_workers(now)
                reason = f"schedule_only: normal (bot_util={bot_util:.2f}, conf={confidence:.2f})"
        else:
            # No pattern data — pure cold start with decay awareness
            workers = self._cold_start_workers(now)
            if bot_util > 0.4:
                workers = max(1, workers - 1)
            reason = f"cold_start_no_probe (bot_util={bot_util:.2f})"

        return WorkerPlan(
            workers=workers,
            reason=reason,
            confidence=confidence,
        )

    def _cold_start_workers(self, t: datetime) -> int:
        """Conservative fallback when pattern data is insufficient.

        2 workers during 9am-6pm weekdays, 4 otherwise.
        """
        local = t.astimezone(self._tz)
        is_weekday = local.weekday() < 5
        is_business_hours = 9 <= local.hour < 18

        if is_weekday and is_business_hours:
            return settings.cold_start_workers_peak
        return settings.cold_start_workers_off
