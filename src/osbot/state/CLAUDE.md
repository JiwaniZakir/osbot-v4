# osbot.state

## Purpose

Async-safe state management and SQLite-backed persistent memory. This is Layer 1 -- nearly every other module depends on it, and it depends on nothing except Layer 0 (config, types, log). It provides two things: (1) `BotState` for the mutable issue queue and active work tracking (state.json), and (2) `MemoryDB` for the temporal, conflict-aware SQLite database (memory.db) that stores repo facts, outcomes, maintainer profiles, repo signals, repo bans, and token usage data.

## Key Interfaces

```python
class BotState:
    """Async-safe state with atomically persisted JSON."""

    async def read(self) -> StateData:
        """Return a frozen snapshot of current state."""

    async def update(self, fn: Callable[[StateData], StateData]) -> StateData:
        """Apply fn to current state, persist, return new state. Holds asyncio.Lock."""

    async def pop_issue(self, predicate: Callable[[Issue], bool]) -> Issue | None:
        """Atomically remove and return first issue matching predicate."""

    async def push_issues(self, issues: list[Issue]) -> None:
        """Add issues to queue, deduplicating by (repo, number)."""


class MemoryDB:
    """SQLite connection manager with temporal conflict resolution."""

    async def init(self) -> None:
        """Open connection, create tables (idempotent)."""

    async def upsert_repo_fact(self, repo: str, key: str, value: str,
                               source: str, confidence: float) -> None:
        """Insert fact, archiving any existing current fact for same (repo, key)."""

    async def get_repo_facts(self, repo: str) -> list[RepoFact]:
        """Return all current (valid_until IS NULL) facts for repo."""

    async def record_outcome(self, repo: str, issue_number: int, pr_number: int | None,
                             outcome: str, failure_reason: str | None, tokens_used: int) -> None:

    async def get_outcomes(self, repo: str | None = None, limit: int = 50) -> list[Outcome]:

    async def ban_repo(self, repo: str, reason: str, days: int, created_by: str) -> None:

    async def is_banned(self, repo: str) -> bool:

    async def get_repo_signals(self, repo: str) -> RepoSignals | None:

    async def upsert_repo_signals(self, repo: str, signals: RepoSignals) -> None:

    async def record_usage_snapshot(self, five_hour: float, seven_day: float,
                                     opus_weekly: float, sonnet_weekly: float) -> None:

    async def record_usage_delta(self, period_start: str, period_end: str,
                                  total_delta: float, bot_delta: float) -> None:

    async def get_user_pattern(self) -> list[PatternSlot]:

    async def update_user_pattern(self, day: int, hour: int, slot: int, delta: float) -> None:
```

## Dependencies

- `osbot.config` -- state directory path, file names
- `osbot.types` -- `StateData`, `Issue`, `RepoFact`, `Outcome`, `RepoSignals`, `PatternSlot`
- `osbot.log` -- structured logging

No imports from any other `osbot.*` package. This is Layer 1.

## Internal Structure

- **`state.py`** -- `BotState` class. Reads/writes `state.json` atomically (write to temp file, `os.replace`). Uses `asyncio.Lock` for all mutations. Holds the issue queue and active work list in memory, persists on every update.

- **`memory.py`** -- `MemoryDB` class. Opens `memory.db` with `aiosqlite`. Creates all 8 tables on init (idempotent `CREATE TABLE IF NOT EXISTS`). Implements temporal conflict resolution: new facts archive old facts by setting `valid_until = now` before inserting. Repo bans checked against `expires_at`. Usage snapshots auto-pruned to 7 days.

- **`migrations.py`** -- One-time migration from v3's `rl_state.json` to `memory.db`. Runs on first boot, idempotent (checks a `migrations_applied` meta table). Converts old `repo_facts` without `valid_from` to `valid_from = migration_time`.

## How to Test

```python
@pytest.fixture
async def memory():
    db = MemoryDB(":memory:")
    await db.init()
    return db

async def test_upsert_archives_old_fact(memory):
    await memory.upsert_repo_fact("owner/repo", "test_cmd", "pytest", "contributing_md", 0.8)
    await memory.upsert_repo_fact("owner/repo", "test_cmd", "make test", "outcome", 0.9)
    facts = await memory.get_repo_facts("owner/repo")
    assert len(facts) == 1
    assert facts[0].value == "make test"

async def test_pop_issue_is_atomic(tmp_path):
    state = BotState(tmp_path / "state.json")
    await state.push_issues([Issue(repo="a/b", number=1, score=7.0)])
    issue = await state.pop_issue(lambda i: i.repo == "a/b")
    assert issue is not None
    assert await state.pop_issue(lambda i: i.repo == "a/b") is None
```

- Use `tmp_path` fixture for `BotState` (needs a real file for atomic rename).
- Use `":memory:"` for `MemoryDB`.
- Test concurrent access with `asyncio.gather` on multiple `update()` calls.

## Design Decisions

1. **asyncio.Lock, not threading.Lock.** v3 used threads. v4 is pure asyncio. The lock protects against concurrent coroutines, not threads.

2. **Atomic JSON writes.** `state.json` is written to a temp file then renamed via `os.replace`. This prevents corruption if the process crashes mid-write.

3. **Temporal facts, not UPSERT.** Old facts are archived (`valid_until` set), never deleted. This preserves history for the learning engine and allows debugging "what did the bot believe at time T?"

4. **Single SQLite connection.** `aiosqlite` wraps SQLite in a background thread. One connection is sufficient -- there are no concurrent processes, only concurrent coroutines within one process.

5. **Repo bans are time-bounded.** Every ban has an `expires_at`. The `is_banned()` check filters by `expires_at > now`. No manual cleanup needed.

6. **No ORM.** Raw SQL with parameterized queries. The schema is simple enough that an ORM adds complexity without value.
