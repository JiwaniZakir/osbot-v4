# osbot.learning

## Purpose

Self-improvement through diagnostics, lesson extraction, and contributor benchmarking. Uses 0-1 Claude calls per 12-hour cycle. The fast diagnostic runs every cycle (0 Claude, < 1 second). The deep diagnostic runs every 12 hours. Event-triggered lessons fire on merges and repeated rejections. This is Layer 4 -- it reads from state (outcomes, traces, repo_facts) and optionally calls gateway for pattern analysis.

## Key Interfaces

```python
class SelfDiagnostics:
    """Per-cycle fast check + 12-hour deep analysis."""

    async def fast_check(self, traces: list[TraceEntry]) -> list[Correction]:
        """Scan last 20 traces for patterns. 0 Claude calls. < 1 second.
        Delegates to safety.CircuitBreaker for pattern detection."""

    async def deep_analysis(self) -> DiagnosticReport:
        """12-hour comprehensive analysis. Arithmetic first, Claude only if needed.
        Computes waste ratio, per-phase failure rates, per-repo submission rates."""


class LessonExtractor:
    """Event-triggered learning from outcomes."""

    async def on_merge(self, repo: str, issue_number: int, pr_number: int) -> None:
        """Extract 'what worked' for this repo. Store as positive lesson in repo_facts."""

    async def on_rejection(self, repo: str, issue_number: int, reason: str) -> None:
        """Track rejection count. On 3rd rejection from same repo,
        extract 'what's different' as negative lesson."""

    async def on_feedback(self, repo: str, maintainer: str, feedback_type: str) -> None:
        """Update maintainer profile with preferences and patterns."""


class ContributorBenchmark:
    """Study top contributors for behavioral patterns. Monthly, no Claude calls."""

    async def benchmark(self, repo: str) -> BenchmarkResult:
        """Analyze top 5 contributors via gh CLI: PR size, response time,
        test inclusion rate, commit message style. Zero Claude cost."""
```

## Dependencies

- `osbot.config` -- deep analysis interval, waste ratio thresholds
- `osbot.types` -- `TraceEntry`, `Correction`, `DiagnosticReport`, `BenchmarkResult`
- `osbot.state` -- `MemoryDB` (read outcomes, traces, repo_facts; write lessons, maintainer profiles, corrections)
- `osbot.gateway` -- `GatewayProtocol` (0-1 Claude calls for deep analysis pattern detection)
- `osbot.safety` -- `CircuitBreaker` (fast diagnostic delegates pattern detection)
- `osbot.log` -- structured logging

## Internal Structure

- **`self_diagnostics.py`** -- `SelfDiagnostics`. Two methods: `fast_check()` runs every cycle and delegates to `CircuitBreaker.fast_diagnostic()` for pattern detection (repo loops, timeout escalation, TOS errors, dead cycles). `deep_analysis()` runs every 12 hours and computes: waste ratio (Claude calls that didn't lead to submission / total calls), per-phase failure rates (which pipeline stage fails most), per-repo submission rates (repos with 5+ attempts and 0 submissions get 14-day ban), parse failure rate. If waste ratio > 30% and arithmetic doesn't explain it, makes 1 Claude call for pattern analysis. All findings logged to `corrections.jsonl`.

- **`lesson_extractor.py`** -- `LessonExtractor`. Event-triggered, not scheduled. `on_merge()`: reads the successful PR's diff, issue, and any feedback thread to extract "what worked" -- stored as a positive `repo_fact` (e.g., "repo prefers small PRs with tests," "maintainer X responds fast to bug fixes"). `on_rejection()`: tracks rejection count per repo. On the 3rd rejection, analyzes all 3 attempts to extract "what's different about this repo" -- stored as a negative lesson (used by `issue_scorer.lesson_adj` to depress scores). `on_feedback()`: updates `maintainer_profiles` with observed preferences (prefers small PRs, requests tests, average response time).

- **`contributor_bench.py`** -- `ContributorBenchmark`. Monthly cadence. For repos in the active pool, uses `gh` CLI to study the top 5 contributors: average PR size, time between issue assignment and PR creation, test inclusion rate, commit message patterns. Zero Claude cost -- pure API data. Results stored in `repo_facts` as benchmark guidance, injected into the implementation prompt so Claude mimics the behavioral patterns of successful contributors.

## How to Test

```python
async def test_deep_analysis_bans_failing_repo(memory):
    # Insert 6 outcomes for same repo, all failures
    for i in range(6):
        await memory.record_outcome("a/b", i, None, "rejected", "scope_creep", 1000)
    diag = SelfDiagnostics(memory=memory, gateway=mock_gateway)
    report = await diag.deep_analysis()
    assert await memory.is_banned("a/b")

async def test_lesson_extraction_on_third_rejection(memory):
    extractor = LessonExtractor(memory=memory)
    await extractor.on_rejection("a/b", 1, "too large")
    await extractor.on_rejection("a/b", 2, "off topic")
    await extractor.on_rejection("a/b", 3, "style mismatch")
    lessons = await memory.get_repo_facts("a/b")
    assert any("lesson" in f.key for f in lessons)

async def test_fast_check_under_one_second():
    traces = [TraceEntry(...) for _ in range(20)]
    diag = SelfDiagnostics(memory=memory, gateway=mock_gateway)
    start = time.monotonic()
    await diag.fast_check(traces)
    assert time.monotonic() - start < 1.0

async def test_benchmark_no_claude_calls(mock_gateway):
    bench = ContributorBenchmark(memory=mock_memory)
    await bench.benchmark("owner/repo")
    assert len(mock_gateway.calls) == 0  # Zero Claude calls
```

- Test deep analysis with synthetic outcomes showing failure patterns.
- Test lesson extraction with sequential rejection events.
- Test fast check performance (must be < 1 second).
- Verify contributor benchmark makes zero Claude calls.

## Design Decisions

1. **Arithmetic first, Claude as last resort.** The deep analysis computes waste ratio, failure rates, and per-repo stats purely from the outcomes table. Only if the numbers show a problem (waste > 30%) AND the arithmetic doesn't explain why, does it spend 1 Claude call on pattern analysis. This keeps the 12-hour learning cycle nearly free.

2. **Event-triggered, not batch.** Lessons are extracted immediately when events occur (merge, 3rd rejection, feedback) rather than in a scheduled batch. This ensures the bot's next attempt on that repo already has the lesson.

3. **3rd rejection threshold.** One rejection could be noise. Two could be bad luck. Three rejections from the same repo is a pattern. The threshold balances learning speed with noise resistance.

4. **Corrections are auditable.** Every self-correction (ban, score adjustment, alert) is logged to `corrections.jsonl` with timestamp, type, and reason. This makes the learning system transparent and debuggable.

5. **Contributor benchmarking uses zero Claude.** Studying top contributors is pure data analysis (PR sizes, timing, test rates). Using Claude here would be wasteful when `gh` CLI provides all the needed data.
