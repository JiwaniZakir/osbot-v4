# osbot.orchestrator

## Purpose

The main asyncio event loop. Schedules phases, manages worker counts, performs startup health checks, and coordinates all engines. This is Layer 5 -- it depends on everything and nothing depends on it. It is the entry point for the bot.

## Key Interfaces

```python
class Orchestrator:
    """Main event loop. Single process, single asyncio loop."""

    async def run(self) -> None:
        """Start the bot. Runs health check, then enters the main loop. Never returns."""

    async def health_check(self) -> None:
        """Startup validation. Blocks until resolved or halts on fatal error."""

    async def cycle(self) -> None:
        """One iteration of the main loop. Runs all due phases."""

    async def shutdown(self) -> None:
        """Graceful shutdown. Cancel workers, flush state, close connections."""
```

### Phase Cadence

```
Phase           Trigger          Workers    Claude Calls
---------------------------------------------------------------
HEALTH CHECK    startup          1          0
DISCOVER        every 30 min     1 (async)  0
CONTRIBUTE      every cycle      <= N *     3 per attempt
ITERATE         every cycle      <= 2       0-2 per PR
REVIEW          every 1 hour     <= 2       1
ENGAGE          every 30 min     <= 1       0-1
MONITOR         every cycle      1 (async)  0
FAST DIAG       every cycle      inline     0
LEARN + DEEP    12h + events     1          0-1
NOTIFY          every 3 min      inline     0-1

* N = tokens.balancer.current_workers (1-5, from token management)
```

Cycle interval: 600 seconds (10 minutes).

### Startup Health Check

```
1. Claude CLI responds (no TOS dialog blocking)
2. GitHub CLI authenticated (gh auth status)
3. OAuth token extractable (for usage probe)
4. memory.db healthy (open + query)
5. active_work cleared (zombie cleanup from previous run)
6. State migration applied (v3 rl_state.json -> memory.db if needed)
```

If TOS dialog detected: halt + alert. If auth failed: halt. If memory.db corrupt: rebuild from traces.jsonl.

## Dependencies

- `osbot.config` -- cycle interval, phase intervals, all timing constants
- `osbot.types` -- all shared types
- `osbot.state` -- `BotState`, `MemoryDB`
- `osbot.gateway` -- `ClaudeGateway`
- `osbot.tokens` -- `TokenBalancer`
- `osbot.discovery` -- `RepoFinder`, `RepoScorer`, `RepoSignalCollector`, `IssueFinder`, `IssueScorer`
- `osbot.pipeline` -- `ContributionPipeline`
- `osbot.iteration` -- `PRMonitor`, `FeedbackReader`, `PatchApplier`
- `osbot.learning` -- `SelfDiagnostics`, `LessonExtractor`, `ContributorBenchmark`
- `osbot.safety` -- `CircuitBreaker`
- `osbot.log` -- structured logging

## Internal Structure

- **`orchestrator.py`** -- `Orchestrator`. The main class. `run()` calls `health_check()`, then enters an infinite loop calling `cycle()` every 600 seconds. Each cycle: (1) run token probe via `balancer.probe_and_update()`, (2) get `current_workers` from balancer, (3) run FAST DIAG inline, (4) check phase timers (discover, engage, review, learn, notify), (5) spawn contribution workers as `asyncio.Task`s up to `current_workers`, (6) spawn iteration workers for PRs with new feedback, (7) await all tasks, (8) record traces. Uses `asyncio.TaskGroup` for structured concurrency. Handles `KeyboardInterrupt` and `SystemExit` via `shutdown()`.

- **`health.py`** -- Startup health check functions. Each check is an independent async function that returns pass/fail. The orchestrator runs them sequentially and halts on any fatal failure. Zombie cleanup: sets all `active_work` entries in state.json to `idle` (a previous crash may have left work "in progress"). Migration: calls `state.migrations.migrate_v3()` if needed.

## How to Test

```python
async def test_health_check_passes(mock_gateway, memory):
    orch = Orchestrator(gateway=mock_gateway, memory=memory, ...)
    await orch.health_check()  # Should not raise

async def test_health_check_halts_on_tos(mock_subprocess):
    mock_subprocess.return_stdout("Terms of Service")  # TOS dialog
    orch = Orchestrator(...)
    with pytest.raises(SystemExit):
        await orch.health_check()

async def test_cycle_respects_worker_count(mock_gateway, memory):
    balancer = MockBalancer(current_workers=2)
    orch = Orchestrator(gateway=mock_gateway, memory=memory, balancer=balancer, ...)
    # Push 5 issues
    await orch.state.push_issues([Issue(...) for _ in range(5)])
    await orch.cycle()
    # Should only have started 2 contribution tasks
    assert len(mock_gateway.calls) <= 2 * 3  # 2 workers * 3 calls each

async def test_shutdown_flushes_state(memory):
    orch = Orchestrator(memory=memory, ...)
    await orch.shutdown()
    # State should be persisted, connections closed
```

- Mock all engines (gateway, balancer, discovery, pipeline, iteration, learning).
- Test that phase timers fire at the correct intervals.
- Test that worker count from balancer is respected.
- Test graceful shutdown (state persisted, no orphaned tasks).

## Design Decisions

1. **Single process, single asyncio loop.** No multiprocessing, no threads. asyncio provides sufficient concurrency for I/O-bound work (subprocess calls to Claude CLI, GitHub API calls). This eliminates all threading bugs from v3.

2. **asyncio.TaskGroup for structured concurrency.** All tasks spawned in a cycle are collected in a TaskGroup. If any task raises an unhandled exception, the group cancels all others. This prevents zombie tasks.

3. **Inline fast diagnostic.** The fast diagnostic runs in the main coroutine, not in a worker. It must complete in < 1 second. This ensures circuit breakers are checked before every contribution attempt.

4. **Health check blocks startup.** The bot does not enter the main loop until all health checks pass. This prevents wasting Claude calls on a misconfigured environment.

5. **Zombie cleanup on startup.** If the bot crashed mid-contribution, `active_work` in state.json may show items as "in progress." The health check resets all active work to idle. The contribution pipeline will re-evaluate these issues on the next cycle.

6. **Graceful shutdown on signals.** SIGTERM and SIGINT trigger `shutdown()`, which cancels running tasks, flushes state to disk, and closes the SQLite connection. This prevents data loss on Docker stop/restart.
