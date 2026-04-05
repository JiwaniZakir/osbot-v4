# osbot.tokens

## Purpose

4-layer token management system that ensures the bot never causes the user to hit a rate limit. Shares a Max 20x subscription with the user's personal Claude usage. This is Layer 3 -- it reads from state (usage data) and hooks into gateway (to record consumption). Its output drives the orchestrator's worker count and model selection.

## Key Interfaces

```python
class TokenBalancer:
    """Public API consumed by orchestrator. Combines all 4 layers."""

    async def probe_and_update(self) -> None:
        """Run L1 probe, update L2 decay, L3 decomposition, L4 schedule. Called every 5 min."""

    @property
    def current_workers(self) -> int:
        """How many contribution workers to run this cycle (1-5)."""

    @property
    def should_prefer_sonnet(self) -> bool:
        """True when Opus 7-day budget > 80% of ceiling."""

    @property
    def headroom(self) -> dict[str, float]:
        """Current headroom for each window: five_hour, seven_day, opus, sonnet."""


class DecayModel:
    """L2: Tracks bot's own consumption with timestamps for rolling window decay."""

    def record(self, tokens: int, model: str) -> None:
        """Record a bot consumption event. Called by gateway after each call."""

    def bot_utilization_at(self, t: datetime) -> float:
        """Predicted bot utilization at time t, accounting for window decay."""

    def effective_headroom(self, probe_headroom: float) -> float:
        """Current headroom adjusted for tokens about to decay off."""


class UsageProbe:
    """L1: Polls OAuth /api/oauth/usage endpoint."""

    async def fetch(self) -> UsageSnapshot:
        """GET /api/oauth/usage with CLI's OAuth token. Returns 4-window utilization."""


class UsageDecomposer:
    """L3: Separates bot vs user consumption."""

    async def decompose(self, prev: UsageSnapshot, curr: UsageSnapshot) -> UsageDelta:
        """total_delta - bot_known_delta = user_delta. Stores in memory.db."""


class PredictiveScheduler:
    """L4: Weekly heatmap + worker plan."""

    async def plan(self, horizon_hours: float = 2.0) -> WorkerPlan:
        """Predict user usage for next N hours, generate worker count plan."""

    @property
    def confidence(self) -> float:
        """0.0-1.0 confidence in the pattern model. Low during cold start."""
```

## Dependencies

- `osbot.config` -- ceilings, max_workers, probe_interval, timezone, estimated_window_capacity
- `osbot.types` -- `UsageSnapshot`, `UsageDelta`, `WorkerPlan`, `PatternSlot`
- `osbot.state` -- `MemoryDB` (reads/writes usage_snapshots, usage_deltas, user_pattern tables)
- `osbot.log` -- structured logging

External: `httpx` (async HTTP for the OAuth probe endpoint).

## Internal Structure

- **`probe.py`** -- `UsageProbe`. Extracts the OAuth token from `~/.claude` credentials. Calls `GET /api/oauth/usage` via `httpx.AsyncClient`. Returns a `UsageSnapshot` with 4 window utilizations (five_hour, seven_day, opus_weekly, sonnet_weekly). Handles auth errors and network failures gracefully (returns last known snapshot).

- **`decay_model.py`** -- `DecayModel`. Maintains an in-memory ledger of `(timestamp, tokens, model)` entries. Prunes entries older than 5 hours. Computes `bot_utilization_at(t)` by summing tokens in the `[t-5h, t]` window. The key insight: if the probe says headroom is 5% but 15% of our tokens are about to decay off, effective headroom is actually 20%.

- **`decomposer.py`** -- `UsageDecomposer`. Between two consecutive probes, computes `user_delta = total_delta - bot_known_delta`. Stores each delta in `memory.db.usage_deltas`. This is how the bot learns the user's consumption pattern without any direct user tracking.

- **`pattern_model.py`** -- `PatternModel`. Builds a weekly heatmap: `(day_of_week, hour, 5min_slot) -> avg_user_delta`. Updated incrementally from each new `UsageDelta`. Stored in `memory.db.user_pattern`. Confidence starts at 0.0 and increases with sample count per slot.

- **`scheduler.py`** -- `PredictiveScheduler`. Uses the pattern model to predict user usage for the next `plan_horizon_hours`. Generates a `WorkerPlan` specifying worker counts per future interval. Blended by confidence: `workers = (confidence * predicted) + ((1-confidence) * fallback)`. Cold start fallback: 2 workers during 9am-6pm weekdays, 4 otherwise.

- **`balancer.py`** -- `TokenBalancer`. Orchestrates all layers. Runs `probe_and_update()` every 5 minutes. Exposes `current_workers` and `should_prefer_sonnet` as simple properties. Applies real-time overrides: if headroom < 5% -> 1 worker regardless of plan. If headroom > 40% and plan says 2 -> allow +1.

## How to Test

```python
async def test_decay_model_prunes_old_entries():
    dm = DecayModel(window_seconds=5 * 3600)
    dm.record(1000, "sonnet")
    # Simulate time passing...
    assert dm.bot_utilization_at(now + timedelta(hours=6)) == 0.0

async def test_cold_start_fallback():
    scheduler = PredictiveScheduler(memory=mock_memory, config=config)
    plan = await scheduler.plan()
    # With no pattern data, should use conservative defaults
    assert 1 <= plan.workers <= 4

async def test_decomposer_separates_usage():
    prev = UsageSnapshot(five_hour=0.10, ...)
    curr = UsageSnapshot(five_hour=0.20, ...)
    decomposer = UsageDecomposer(decay_model=dm, memory=mock_memory)
    delta = await decomposer.decompose(prev, curr)
    assert delta.user_delta == delta.total_delta - delta.bot_delta
```

- Mock the OAuth endpoint with a canned JSON response for probe tests.
- Test decay model with synthetic timestamps (inject a clock function).
- Test scheduler's cold start vs warm behavior by controlling pattern data.

## Design Decisions

1. **4 layers, not 1.** Each layer solves a specific problem. The probe gives ground truth but no prediction. The decay model gives near-future accuracy. The decomposer attributes usage. The scheduler plans ahead. Removing any layer degrades behavior.

2. **Conservative cold start.** With no data, the bot assumes the user is active during business hours. This avoids the worst case (bot uses all capacity right before the user needs it). The fallback is aggressively overridden as pattern data accumulates.

3. **Decay model is in-memory only.** The 5-hour window of bot consumption entries does not need persistence -- it rebuilds naturally after restart (the probe provides ground truth). Persistence would add complexity for zero value.

4. **Confidence-blended scheduling.** Rather than a hard cutover from "cold start mode" to "prediction mode," the blend ensures a smooth transition. At 30% confidence, the bot is 70% conservative + 30% predictive.

5. **Real-time override trumps plan.** The plan is a forecast. If the probe shows headroom collapsing (user started early), the override kicks in immediately. This prevents the plan from being stale.

6. **Opus conservation is automatic.** When the 7-day Opus budget exceeds 80% of ceiling, `should_prefer_sonnet` flips. The critic call (normally Opus) degrades to Sonnet. This preserves Opus budget for the user's complex Claude Code sessions without manual intervention.
