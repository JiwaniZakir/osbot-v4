"""Public API: TokenBalancer.

Orchestrates all four layers (probe, decay, decompose, schedule)
into a single interface consumed by the orchestrator.  Exposes
``current_workers``, ``should_prefer_sonnet``, and ``headroom``.
Implements ``BalancerProtocol``.

Real-time override: if probe headroom < 5%, force 1 worker regardless
of plan.  Cold start fallback: 2 workers peak / 4 off-hours when
confidence < 0.3.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from osbot.config import settings
from osbot.log import get_logger
from osbot.tokens.decay import DecayModel
from osbot.tokens.decomposer import Decomposer
from osbot.tokens.pattern import PatternModel
from osbot.tokens.probe import probe
from osbot.tokens.scheduler import Scheduler

if TYPE_CHECKING:
    from osbot.types import MemoryDBProtocol, UsageSnapshot, WorkerPlan

logger = get_logger("tokens.balancer")

_HEADROOM_EMERGENCY = 0.05  # 5% -> force 1 worker
_HEADROOM_GENEROUS = 0.40   # 40% -> allow +1 worker


class Balancer:
    """Token management balancer — public API for the orchestrator.

    Call ``update()`` every probe interval (default 5 min).
    Read ``current_workers`` and ``should_prefer_sonnet`` each cycle.
    """

    # Probe no more than once per hour (endpoint has ~1/hr rate limit)
    _PROBE_INTERVAL_SEC = 3600

    def __init__(self, memory: MemoryDBProtocol, oauth_token: str | None = None) -> None:
        self._memory = memory
        self._oauth_token = oauth_token

        # Sub-systems
        self._decay = DecayModel()
        self._decomposer = Decomposer(self._decay, memory)
        self._pattern = PatternModel(memory)
        self._scheduler = Scheduler(self._pattern, self._decay)

        # State
        self._last_snapshot: UsageSnapshot | None = None
        self._last_plan: WorkerPlan | None = None
        self._workers: int = settings.cold_start_workers_off
        self._prefer_sonnet: bool = False
        self._initialized = False
        self._last_probe_time: float = 0.0  # epoch seconds
        self._consecutive_probe_failures: int = 0

    # -- BalancerProtocol implementation -------------------------------------

    @property
    def current_workers(self) -> int:
        """How many contribution workers to run this cycle (1-5)."""
        return self._workers

    @property
    def should_prefer_sonnet(self) -> bool:
        """True when Opus 7-day budget > 80% of ceiling."""
        return self._prefer_sonnet

    @property
    def headroom(self) -> dict[str, float]:
        """Current headroom for each window."""
        snap = self._last_snapshot
        if snap is None:
            return {"five_hour": 1.0, "seven_day": 1.0, "opus": 1.0, "sonnet": 1.0}
        return {
            "five_hour": max(settings.five_hour_ceiling - snap.five_hour, 0.0),
            "seven_day": max(settings.seven_day_ceiling - snap.seven_day, 0.0),
            "opus": max(settings.opus_ceiling - snap.opus_weekly, 0.0),
            "sonnet": max(1.0 - snap.sonnet_weekly, 0.0),
        }

    async def update(self) -> None:
        """Run L1 probe (hourly), L2 decay, L3 decomposition, L4 schedule.

        Called every cycle (10 min) by the orchestrator.  The probe runs
        at most once per hour to respect the endpoint's rate limit.
        Between probes, the decay model and scheduler still update
        worker counts based on our own consumption and the learned pattern.
        """
        import time

        if not self._initialized:
            await self._pattern.load()
            self._initialized = True

        # L1: Probe — only once per hour (endpoint rate limit ~1/hr)
        now_epoch = time.monotonic()
        should_probe = (now_epoch - self._last_probe_time) >= self._PROBE_INTERVAL_SEC

        snapshot = self._last_snapshot  # Default: reuse last

        if should_probe:
            fresh = await probe(self._oauth_token)
            self._last_probe_time = now_epoch
            if fresh is not None:
                snapshot = fresh
                self._consecutive_probe_failures = 0
                logger.info("balancer_probe_ok",
                            five_hour=round(fresh.five_hour, 3),
                            seven_day=round(fresh.seven_day, 3))
            else:
                self._consecutive_probe_failures += 1
                logger.warning("balancer_probe_failed",
                               consecutive_failures=self._consecutive_probe_failures)

        if snapshot is None:
            # Never got any probe data — use schedule-only mode
            plan = await self._scheduler.plan_without_probe()
            self._workers = plan.workers
            logger.info("balancer_schedule_only",
                        workers=plan.workers, reason=plan.reason)
            return

        # Persist snapshot
        await self._persist_snapshot(snapshot)

        # L3: Decompose (requires a previous snapshot)
        if self._last_snapshot is not None:
            delta = await self._decomposer.decompose(
                self._last_snapshot, snapshot, settings.estimated_window_capacity
            )
            # L4: Update pattern model
            await self._pattern.record(delta)

        self._last_snapshot = snapshot

        # L4: Schedule
        plan = await self._scheduler.plan(snapshot)
        self._last_plan = plan
        workers = plan.workers

        # Real-time overrides
        five_hour_headroom = settings.five_hour_ceiling - snapshot.five_hour
        effective = self._decay.effective_headroom(five_hour_headroom)

        if effective < _HEADROOM_EMERGENCY:
            workers = 1
            logger.warning(
                "balancer_emergency",
                headroom=round(effective, 3),
                original_plan=plan.workers,
            )
        elif effective > _HEADROOM_GENEROUS and plan.workers <= 2:
            workers = min(plan.workers + 1, settings.max_workers)
            logger.info(
                "balancer_generous_boost",
                headroom=round(effective, 3),
                boosted_to=workers,
            )

        # Also clamp by 7-day headroom
        seven_day_headroom = settings.seven_day_ceiling - snapshot.seven_day
        if seven_day_headroom < _HEADROOM_EMERGENCY:
            workers = 1
            logger.warning("balancer_7day_emergency", headroom=round(seven_day_headroom, 3))

        self._workers = workers

        # Opus conservation: prefer Sonnet when Opus budget is > 80% of ceiling
        self._prefer_sonnet = snapshot.opus_weekly > (settings.opus_ceiling * 0.80)

        logger.info(
            "balancer_updated",
            workers=self._workers,
            prefer_sonnet=self._prefer_sonnet,
            five_hour=round(snapshot.five_hour, 3),
            seven_day=round(snapshot.seven_day, 3),
            opus=round(snapshot.opus_weekly, 3),
            confidence=round(plan.confidence, 2),
            plan_reason=plan.reason,
        )

    def record_consumption(self, tokens: int, model: str) -> None:
        """Record bot token consumption.  Called by the gateway after each call."""
        self._decay.record(tokens, model)

    # -- internals -----------------------------------------------------------

    async def _persist_snapshot(self, snapshot: UsageSnapshot) -> None:
        """Store the snapshot in the usage_snapshots table."""
        try:
            await self._memory.execute(
                """
                INSERT INTO usage_snapshots (ts, five_hour, seven_day, opus_weekly, sonnet_weekly)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot.ts,
                    snapshot.five_hour,
                    snapshot.seven_day,
                    snapshot.opus_weekly,
                    snapshot.sonnet_weekly,
                ),
            )
            # Prune snapshots older than 7 days
            cutoff = datetime.now(UTC).isoformat()
            await self._memory.execute(
                "DELETE FROM usage_snapshots WHERE ts < datetime(?, '-7 days')",
                (cutoff,),
            )
        except Exception as exc:
            logger.warning("balancer_persist_failed", error=str(exc))
