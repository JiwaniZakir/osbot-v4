# osbot.safety

## Purpose

Anti-spam enforcement and circuit breaker logic. Prevents the bot from spamming repos, looping on the same error, or continuing when something is fundamentally broken. This is Layer 2 -- it reads from state (repo_bans, outcomes, traces) and is called by preflight and the fast diagnostic. No Claude calls.

## Key Interfaces

```python
class AntiSpam:
    """Prevent excessive activity on any single repo or org."""

    async def check(self, repo: str) -> SpamCheckResult:
        """Check if we've exceeded activity thresholds for this repo/org."""

    async def record_activity(self, repo: str, activity_type: str) -> None:
        """Record a PR submission, comment, or claim for rate tracking."""


class CircuitBreaker:
    """Detect and act on failure patterns."""

    async def fast_diagnostic(self, traces: list[TraceEntry]) -> list[Correction]:
        """Scan last 20 traces. Returns corrections to apply (bans, score adjustments, alerts).
        Called every cycle. Must complete in < 1 second."""

    async def check_repo(self, repo: str) -> bool:
        """Quick check: is this repo safe to attempt? Checks bans table."""
```

### Circuit Breaker Rules

```
Signal                                  -> Action
------------------------------------------------------------------------
Same repo, same error, 3+ times        -> Ban 7 days + clear queue entries
Planning timeout 2x on same repo       -> Score -2.0, ban at 4x
5 consecutive failures (any repo)       -> Ban 7 days
Language/domain filter fails            -> Permanent removal from pool
TOS or auth error detected              -> Halt all work, alert
15+ consecutive cycles with 0 submits   -> Force discovery refresh
```

## Dependencies

- `osbot.config` -- thresholds (if any overrides needed)
- `osbot.types` -- `TraceEntry`, `Correction`, `SpamCheckResult`
- `osbot.state` -- `MemoryDB` (read/write repo_bans, read outcomes and traces)
- `osbot.log` -- structured logging

## Internal Structure

- **`anti_spam.py`** -- `AntiSpam`. Tracks activity per repo and per org using the outcomes table. Enforces: no more than N submissions per repo per day, no more than M submissions per org per day. These are not artificial caps on the bot -- they prevent the bot from appearing as a spam bot to GitHub and to maintainers. Blacklists orgs that have publicized problems with bots.

- **`circuit_breaker.py`** -- `CircuitBreaker`. The `fast_diagnostic()` method runs every cycle (inline, not in a worker). It scans the last 20 trace entries looking for the patterns listed above. When a pattern matches, it creates a `Correction` object (ban, score adjustment, alert, or halt) and applies it. All corrections are also logged to `corrections.jsonl` for audit. The `check_repo()` method is a quick lookup against `memory.db.repo_bans` -- called by preflight before any Claude call.

## How to Test

```python
async def test_circuit_breaker_bans_after_3_same_errors(memory):
    traces = [
        TraceEntry(repo="a/b", error="lint_failed", ...),
        TraceEntry(repo="a/b", error="lint_failed", ...),
        TraceEntry(repo="a/b", error="lint_failed", ...),
    ]
    cb = CircuitBreaker(memory=memory)
    corrections = await cb.fast_diagnostic(traces)
    assert any(c.type == "ban_repo" and c.repo == "a/b" for c in corrections)
    assert await memory.is_banned("a/b")

async def test_circuit_breaker_halts_on_tos_error(memory):
    traces = [TraceEntry(repo="a/b", error="tos_dialog_detected", ...)]
    cb = CircuitBreaker(memory=memory)
    corrections = await cb.fast_diagnostic(traces)
    assert any(c.type == "halt" for c in corrections)

async def test_anti_spam_limits_per_org(memory):
    spam = AntiSpam(memory=memory)
    for i in range(10):
        await spam.record_activity(f"big-org/repo-{i}", "pr_submitted")
    result = await spam.check("big-org/repo-11")
    assert not result.allowed
```

- Feed synthetic `TraceEntry` lists to test pattern detection.
- Use in-memory `MemoryDB` for ban persistence tests.
- Test edge cases: exactly-at-threshold, mixed errors, timeout escalation.

## Design Decisions

1. **Fast diagnostic runs inline, not in a worker.** It scans 20 trace entries and does simple pattern matching. Must complete in under 1 second. No Claude calls, no I/O beyond SQLite reads.

2. **All corrections logged.** `corrections.jsonl` is an append-only audit trail. If the bot bans a repo, the correction entry explains why. This is essential for debugging and for building confidence that the self-correction system works.

3. **Checked in preflight BEFORE any Claude call.** A banned repo is caught before spending any tokens. The ban check is a single SQLite query.

4. **TOS/auth errors halt everything.** These are not recoverable by the bot. Continuing would waste resources and could violate terms. The only correct action is to stop and alert.

5. **Permanent removal for domain violations.** If a repo passes discovery but fails a domain check in preflight (shouldn't happen, but defense in depth), it is permanently removed from the pool. No second chances for off-domain repos.
