# osbot v4 Gap Analysis

**Date:** 2026-03-25
**Analyst role:** Senior Product Manager
**Scope:** v4_plan.md vs. actual codebase (`src/osbot/`)

---

## 1. Completion Matrix

### 1.1 Architecture (Plan Section 2)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| asyncio event loop, single process | **Done** | `orchestrator/loop.py` uses `asyncio.TaskGroup`, no threads | Matches plan exactly |
| Token management system (4 layers) | **Done** | `tokens/probe.py`, `decay.py`, `decomposer.py`, `pattern.py`, `scheduler.py`, `balancer.py` | All 4 layers implemented and wired |
| Discovery engine (0 Claude calls) | **Done** | `discovery/__init__.py` orchestrates full pipeline | Signal enrichment, scoring, issue finding all present |
| Contribution engine (3 calls) | **Done** | `pipeline/run.py` with implement, critic, PR writer | Plus soft-retry on critic rejection (not in plan -- see 4.1) |
| Learning engine | **Partial** | `learning/diagnostics.py`, `lessons.py`, `benchmark.py` | Fast diagnostic done. Deep 12h analysis NOT implemented (no scheduled trigger). Benchmark exists but not wired to orchestrator cadence |
| State layer (SQLite + JSON + JSONL) | **Done** | `state/db.py`, `bot_state.py`, `traces.py`, `migrations.py` | All 8 tables from plan. Extra: `repo_fact_index` table, `summary` column on outcomes |
| Claude gateway (Agent SDK) | **Done** | `gateway/claude.py` | Streams events, captures tool trace, timeout, usage recording |
| Priority queue | **Done** | `gateway/priority.py` | 8-level priority, async PQ, FIFO within level |
| GitHub CLI wrapper (async) | **Done** | `gateway/github.py` | `asyncio.create_subprocess_exec`, GraphQL via `gh api graphql`, rate limit detection |

### 1.2 Token Management (Plan Section 3)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| L1: Utilization probe (OAuth endpoint) | **Done** | `tokens/probe.py` | Reads OAuth token from `~/.claude`, polls `/api/oauth/usage` |
| L2: Window decay model | **Done** | `tokens/decay.py` | In-memory ledger, `effective_headroom()` |
| L3: Usage decomposer | **Done** | `tokens/decomposer.py` | `total - bot = user`, persisted to `usage_deltas` |
| L4: Predictive scheduler | **Done** | `tokens/scheduler.py` | Weekly heatmap, cold start fallback, blended confidence |
| L4: Pattern model | **Done** | `tokens/pattern.py` | 2016-slot weekly heatmap, incremental updates |
| Cold start fallback | **Done** | `scheduler.py` lines 108-119 | 2 workers peak / 4 off-hours |
| Real-time override (headroom < 5%) | **Done** | `balancer.py` lines 121-140 | Emergency + generous boost |
| Opus conservation (prefer sonnet) | **Done** | `balancer.py` line 145 | Triggers at 80% of `opus_ceiling` |
| Probe interval 5 min | **Done** | `config.py` `probe_interval_sec=300` | Note: probe runs every cycle (10 min), not every 5 min -- see risk 1.3 |
| Configuration (8 values) | **Done** | `config.py` lines 63-77 | All 8 plan values present |

### 1.3 Discovery Engine (Plan Section 4)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Dynamic repo pool (no hardcoded list) | **Done** | `repo_finder.py` uses `gh search repos` | Topics, stars, push recency filters |
| Signal enrichment (merge rate, response time, CI) | **Done** | `repo_signals.py` | 7-day cache TTL, external merge rate, response hours |
| Signal-based repo scoring | **Done** | `repo_scorer.py` | 5.0 base + 6 adjustments, 0-10 clamped |
| Issue scoring (4 adjustments) | **Done** | `issue_scorer.py` | `repo_adj`, `label_adj`, `quality_adj`, `lesson_adj` + bonus `benchmark_adj` |
| Maintainer confirmation +1.50 | **Done** | `issue_scorer.py` line 170 | Uses `settings.maintainer_confirmed_bonus` |
| Blended merge rate (our + external) | **Done** | `issue_scorer.py` lines 94-127 | 70/30 blend |
| GraphQL-enriched issue search | **Done** | `issue_finder.py`, `intel/graphql.py` | Issue detail with comments, timeline, reactions |
| No-AI-policy detection | **Done** | `intel/policy.py`, `safety/domain.py` | 5 regex patterns, scans CONTRIBUTING.md |
| Domain filter (language + topic) | **Done** | `safety/domain.py` | AND condition: language match + topic match |
| Active pool cap at 100 | **Done** | `config.py` `active_pool_max=100` | |
| Repo score threshold >= 4.0 | **Done** | `config.py` `repo_score_threshold=4.0` | |
| Auto-exclude > 100K stars | **Missing** | `config.py` `repo_max_stars=30_000` | Plan says >100K excluded but config caps at 30K (more conservative, acceptable) |
| Auto-exclude > 50% closed-without-merge | **Missing** | Not checked anywhere | Plan specifies this filter; `repo_scorer.py` does not implement it as a hard exclusion |
| Trending repo discovery | **Missing** | Not present | Plan Phase 6, item 36 |

### 1.4 Contribution Pipeline (Plan Section 5)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Preflight gates (no Claude) | **Done** | `pipeline/preflight.py` | 7 checks: banned, spam, prior outcome, duplicate PR, issue open, CLA, maintainer active |
| Assignment flow (state machine) | **Done** | `pipeline/assignment.py` | `check_assignment` / `request_assignment` / `poll_assignment` |
| Assignment timeout 72h | **Done** | `config.py` `assignment_timeout_hours=72` | |
| Call #1: Implementation (sonnet, 180s) | **Done** | `pipeline/implementer.py` | TASK/FORBIDDEN prompt, max_turns=25, style notes injected |
| Quality gates (no Claude) | **Done** | `pipeline/quality.py` | diff lines, file count, reformat detection, lint, tests, commit msg |
| Diff <= 50 lines | **Done** | `config.py` `max_diff_lines=50` | Hard gate |
| <= 3 files changed | **Done** | `config.py` `max_files_changed=3` | Hard gate |
| Call #2: Critic (opus, 120s) | **Done** | `pipeline/critic.py` | MAR-style with tool trace, strict JSON, opus/sonnet fallback |
| Critic HARD GATE | **Done** | `pipeline/run.py` lines 180-231 | REJECT = rejected (after possible soft retry) |
| Call #3: PR description (sonnet, 60s) | **Done** | `pipeline/pr_writer.py` | `Closes #N` template literal, specificity validation |
| Closes #N template literal | **Done** | `pr_writer.py` line 112 | Code-injected, not Claude-generated |
| Test output in PR description | **Partial** | PR writer prompt requests "Testing" section | Not validated that actual test output is present |
| Submission (fork, push, gh pr create) | **Done** | `pipeline/submitter.py` | Fork handling, branch naming, remote setup |
| Humanizer delay 15-45 min | **Partial** | `timing/humanizer.py` exists | But `submitter.py` does NOT call the humanizer -- delay is not wired |
| Pre-contribution engagement (comment before PR) | **Missing** | Not implemented | Plan Section 12 and Phase cadence mention this; engage phase is a stub |
| PR description structural variation (style seed) | **Missing** | `comms/comments.py` has `_CLAIM_TEMPLATES` but no style seed for PR body | Plan Section 12 mentions `apply_style_seed` |
| No artificial limits (weekly caps, cooldowns) | **Done** | `anti_spam.py` only has org blacklist | Token management is sole gate |

### 1.5 PR Iteration Loop (Plan Section 6)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| PR monitor (GraphQL polling) | **Done** | `iteration/monitor.py` | Comments, reviews, CI status, merge state, conflicts |
| Call #4: Feedback reader (sonnet, 60s) | **Done** | `iteration/feedback.py` | 6-type classification, action item extraction |
| Call #5: Patch applier (sonnet, 120s) | **Done** | `iteration/patcher.py` | Narrowly-scoped prompt, quality gates, push |
| Safety: max 3 rounds | **Done** | `patcher.py` line 30, `config.py` `max_iteration_rounds=3` | |
| Safety: size growth >120% | **Done** | `patcher.py` lines 64-68 | |
| Safety: stop on merge conflicts | **Done** | `patcher.py` line 34 | |
| Feedback response delay 30m-4h | **Partial** | `humanizer.py` has `delay_feedback_response()` | But NOT called in `_phase_iterate` or `patcher.py` |
| Response-only path for questions | **Partial** | `feedback.py` sets `should_patch=False` for questions | But no response comment is posted for question-type feedback |
| Priority queue: feedback > new contributions | **Done** | `types.py` `Priority.FEEDBACK_RESPONSE = 0` | But priority queue is not used in the main loop -- gateway calls are direct |
| Iterate phase wired to orchestrator | **Done** | `loop.py` `_phase_iterate` | Runs every cycle |
| Lesson recording on rejection | **Done** | `loop.py` lines 99-103 calls `on_rejection` | |

### 1.6 Learning Engine (Plan Section 7)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Per-cycle fast diagnostic (0 Claude) | **Done** | `learning/diagnostics.py` | Loop detection, timeout, TOS/auth halt, dead cycles |
| 12-hour deep analysis | **Missing** | No scheduled trigger in `loop.py` | `learn_interval_sec=43200` is configured but the learn phase is never called |
| Event-triggered: on_merge lesson | **Done** | `learning/lessons.py` `on_merge()` | Stores positive lesson in repo_facts |
| Event-triggered: on 3rd rejection | **Done** | `learning/lessons.py` `on_rejection()` | Pattern matching on failure reasons |
| Event-triggered: on feedback | **Done** | `learning/lessons.py` `on_feedback()` | Updates maintainer_profiles |
| Maintainer profile updates | **Partial** | `on_feedback()` exists but is never called | The iterate phase does not call it after processing feedback |
| Corrections logged to corrections.jsonl | **Done** | `traces.py` `write_correction()`, wired in `loop.py` | |
| Contributor benchmarking (monthly) | **Partial** | `learning/benchmark.py` exists | Not wired to any scheduler/timer in the orchestrator |
| Waste ratio computation | **Missing** | Not implemented | Plan says deep analysis computes waste ratio |
| Per-phase failure rate analysis | **Missing** | Not implemented | Part of deep analysis |
| Per-repo submission rate analysis | **Missing** | Not implemented | Part of deep analysis |

### 1.7 Self-Diagnostics / Circuit Breakers (Plan Section 8)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Same repo, same error, 3+ -> ban 7d | **Done** | `diagnostics.py` + `circuit_breaker.py` | |
| Timeout 2x -> score -2.0, ban at 4x | **Done** | `diagnostics.py` + `circuit_breaker.py` | |
| 5 consecutive failures -> ban 7d | **Done** | `circuit_breaker.py` `record_failure()` | |
| Language/domain filter fails -> permanent | **Partial** | `domain.py` filters in discovery | But no "permanent removal" mechanism distinct from normal filtering |
| TOS/auth error -> halt + alert | **Done** | `diagnostics.py` lines 70-89 | Returns halt correction |
| Checked in preflight BEFORE Claude | **Done** | `preflight.py` calls `can_attempt_repo()` first | |
| Persisted in repo_bans (survives restart) | **Done** | `circuit_breaker.py` writes to SQLite | |

### 1.8 Memory (Plan Section 9)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| repo_facts (temporal, conflict-aware) | **Done** | `migrations.py` migration 1, `db.py` `set_repo_fact()` | Archives old, inserts new |
| outcomes table | **Done** | Full schema implemented | |
| maintainer_profiles table | **Done** | Schema present | Writes are minimal (lessons.py `on_feedback`) |
| repo_signals table (7-day TTL) | **Done** | Used by `repo_signals.py` | |
| repo_bans table | **Done** | Used by circuit_breaker | |
| usage_snapshots table | **Done** | Used by balancer | |
| usage_deltas table | **Done** | Used by decomposer | |
| user_pattern table | **Done** | Used by pattern model | |
| Conflict resolution (archive + insert) | **Done** | `db.py` `set_repo_fact()` | |
| Progressive disclosure (fact index) | **Done** | `db.py` `get_fact_index()`, `repo_fact_index` table | Not in original plan -- added enhancement |

### 1.9 Claude Gateway (Plan Section 10)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Agent SDK integration | **Done** | `gateway/claude.py` | Streams `SDKResultMessage`, `SDKAssistantMessage` |
| Timeout enforcement | **Done** | `asyncio.timeout(timeout)` | |
| Tool trace capture | **Done** | Captures tool name + input from `content` blocks | |
| Token recording callback | **Done** | `on_call_complete` parameter | |
| Priority queue in gateway | **Partial** | `priority.py` exists with full implementation | But `ClaudeGateway.invoke()` does NOT use it -- calls execute immediately without queuing |
| async subprocess for gh/git | **Done** | `github.py` uses `asyncio.create_subprocess_exec` exclusively | |

### 1.10 Startup Health Check (Plan Section 11)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Claude CLI responds (no TOS) | **Done** | `health.py` `_check_claude()` | |
| GitHub CLI authenticated | **Done** | `health.py` `_check_github()` | |
| OAuth token extractable | **Missing** | Not explicitly checked at startup | Probe handles it at runtime, but plan says startup should verify |
| memory.db healthy | **Done** | `health.py` `_check_memory_db()` | |
| active_work cleared (zombies) | **Done** | `health.py` `_clear_zombies()` + `loop.py` `state.clear_active()` | |
| State migration (rl_state.json -> memory.db) | **Missing** | `migrations.py` only handles schema versioning | No v3 `rl_state.json` migration code |

### 1.11 Anti-Detection (Plan Section 12)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| 40+ banned AI phrases | **Done** | `comms/phrases.py` | 50+ phrases across 7 categories |
| Humanizer delay (15-45 min PR) | **Partial** | `timing/humanizer.py` has the code | NOT wired into submitter.py |
| CLA compliance | **Done** | `compliance/cla.py` | Multi-strategy detection |
| Bot-detection quarantine | **Missing** | Not implemented | Plan mentions this from v3 |
| Pre-contribution engagement | **Missing** | Engage phase is a stub | |
| PR description structural variation | **Missing** | No `apply_style_seed` implementation | |
| Feedback response delay | **Partial** | Humanizer has it | Not called by iterate phase |
| Test output in PR description | **Partial** | Prompt asks for it | Not validated |
| No-AI-policy scanner | **Done** | `intel/policy.py`, `safety/domain.py` | |

### 1.12 Phase Cadence (Plan Section 13)

| Phase | Status | Evidence | Notes |
|---|---|---|---|
| HEALTH CHECK (startup) | **Done** | `orchestrator/health.py` | |
| DISCOVER (30 min) | **Done** | `loop.py` with `discover_interval_sec` timer | |
| CONTRIBUTE (every cycle) | **Done** | `loop.py` `_phase_contribute()` | |
| ITERATE (every cycle) | **Done** | `loop.py` `_phase_iterate()` | |
| REVIEW (1 hour) | **Stub** | `loop.py` `_phase_review()` logs placeholder | |
| ENGAGE (30 min) | **Stub** | `loop.py` `_phase_engage()` logs placeholder | |
| MONITOR (every cycle) | **Done** | `loop.py` `_phase_monitor()` | |
| FAST DIAG (every cycle) | **Done** | `loop.py` calls `fast_diagnostic()` | |
| LEARN + DEEP DIAG (12h) | **Missing** | No `_phase_learn` in loop.py | `learn_interval_sec` configured but unused |
| NOTIFY (3 min) | **Stub** | `loop.py` `_phase_notify()` logs placeholder | |

### 1.13 Deployment (Plan Section 14)

| Feature | Status | Evidence | Notes |
|---|---|---|---|
| Dockerfile with Claude CLI + gh | **Done** | `deploy/Dockerfile` | Multi-stage build, node:18 for CLI |
| docker-compose.yml | **Done** | `deploy/docker-compose.yml` | Resource limits, volume mounts |
| Volumes (credentials, gh, state) | **Done** | Matches plan exactly | Reuses v3 credential volumes |
| Health check | **Done** | `deploy/health_check.py` | Docker HEALTHCHECK configured |
| Agent SDK installed | **Done** | Dockerfile `pip install claude-agent-sdk` | |
| v3 state migration | **Missing** | No migration script | Plan says `rl_state.json` migrated on first boot |

### 1.14 Build Sequence Phases (Plan Section 15)

| Phase | Status | Notes |
|---|---|---|
| Phase 1: Stop the Bleeding (Days 1-3) | **Done** | Domain enforcement, circuit breakers, no artificial limits, fast diagnostic, health check |
| Phase 2: Foundation (Days 4-8) | **Done** | Token mgmt L1/L2, Agent SDK, SQLite schema, async subprocess, dynamic workers |
| Phase 3: Discovery Upgrade (Days 9-12) | **Done** | repo_signals, GraphQL client, repo scorer, issue scorer v2, AI policy, L3 decomposer |
| Phase 4: Pipeline + Assignment (Days 13-17) | **Done** | Implementation prompt, quality gates, critic with trace, Closes #N, assignment flow |
| Phase 5: Iteration Loop (Days 18-22) | **Partial** | PR monitor, feedback reader, patch applier done. Response delay NOT wired. Priority queue NOT used for ordering. Question response path incomplete |
| Phase 6: Learning + Prediction (Days 23-28) | **Partial** | L4 scheduler done. Deep diagnostic, lesson extraction code exists but 12h trigger missing. Benchmark exists but not scheduled. Trending repo discovery missing |

---

## 2. Features in Code but NOT in Plan (Scope Creep)

| Feature | Location | Assessment |
|---|---|---|
| Soft retry on critic rejection | `pipeline/run.py` lines 181-231 | **Low risk.** Allows one re-implementation for non-correctness issues. Not in plan but a reasonable improvement over the hard "REJECT = permanent" rule. Could waste tokens if not carefully bounded |
| Progressive disclosure / fact index | `state/db.py`, `state/migrations.py` (migration 2) | **Beneficial.** Reduces token waste by injecting only a compact index into prompts instead of all facts. Not in plan but aligns with efficiency goals |
| Outcome summaries (compressed narratives) | `state/db.py` `record_outcome_with_summary()` | **Beneficial.** Richer data for lesson synthesis. Zero cost (assembled from structured data, not Claude) |
| Benchmark adjustment in issue scoring | `issue_scorer.py` `_compute_benchmark_adj()` | **Low risk.** Adds a 5th adjustment (`benchmark_adj`) to the planned 4. Bonus is [0, +0.5] -- small and capped |
| `max_turns` parameter on Claude calls | `gateway/claude.py`, all callers | **Beneficial.** Controls Claude's autonomy per call type. Implementer gets 25 turns, critic gets 1 |
| Codebase analyzer via API (not workspace) | `intel/codebase.py` | **Different from plan.** Plan says scan cloned workspace; implementation fetches via `gh api`. Works but means analysis happens before cloning |

---

## 3. Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Humanizer delays not wired** -- the bot submits PRs and feedback responses instantly, making it trivially detectable as a bot | High | Critical | Wire `humanizer.delay_pr_creation()` into `submitter.py` before `_create_pr()`. Wire `humanizer.delay_feedback_response()` into `_phase_iterate` before `apply_patch` |
| R2 | **Priority queue not used** -- feedback responses execute at the same priority as new contributions, violating the core principle that a waiting maintainer is the highest-value moment | High | High | Either route all gateway calls through `PriorityCallQueue` (as plan intended), or restructure the loop so iterate runs before contribute (simpler) |
| R3 | **Review + Engage + Notify phases are stubs** -- no pre-contribution engagement means the bot has no comment history before submitting PRs, which is a strong bot signal | High | High | At minimum, implement engage phase (comment on issue before PR). Review and notify can be deferred |
| R4 | **12h deep analysis never triggers** -- learning never runs; repos with 5+ failures never get auto-banned by the deep diagnostic; waste ratio is never computed | Medium | Medium | Add a `last_learn` timer to the main loop and call `deep_analysis()` when elapsed. Pattern model never gets advanced correction signals |
| R5 | **Trace buffer in loop.py is never populated** -- `recent_traces` stays empty, so `fast_diagnostic` always receives `[]` | High | High | After each contribution result, append a `Trace` to `recent_traces`. Currently the buffer is declared but never written to |
| R6 | **No tests** -- `tests/conftest.py` is empty; zero test files exist | High | High | Any change risks breaking the pipeline. The plan specified test strategies for every module. Even basic smoke tests for preflight, scoring, and gateway would catch regressions |
| R7 | **OAuth usage endpoint may not exist** -- `/api/oauth/usage` is undocumented and may not return the expected JSON structure, causing the entire token management system to operate on fallback (cold start) indefinitely | Medium | Medium | Add graceful degradation logging. If probe returns `None` for 10+ consecutive attempts, log a clear warning and document alternative approaches |
| R8 | **github_username not set by default** -- `config.py` has `github_username: str = ""`, but duplicate PR check, assignment polling, and fork-based submission all depend on it | High | High | Add to startup health check. Fail if empty. docker-compose sets `OSBOT_GH_USERNAME` but the env var prefix is `OSBOT_`, so it should be `OSBOT_GITHUB_USERNAME` |
| R9 | **No graceful shutdown** -- plan specifies `shutdown()` method with signal handlers, but `loop.py` has no SIGTERM/SIGINT handling, no connection cleanup, no state flush | Medium | Medium | Add signal handlers wrapping `db.close()` and `state._flush()` |
| R10 | **Workspace cleanup on iteration** -- `_phase_iterate` references `settings.workspaces_dir` for patch workspace, but it creates a path from PR data without cloning the repo first. `apply_patch` tries to `checkout` a branch that may not exist locally | Medium | High | The iterate workspace setup is incomplete -- needs clone + checkout before patching |

---

## 4. Recommended Priority Order for Remaining Work

### Priority 1: Ship Blockers (Must fix before first deploy)

1. **Wire humanizer delays into submitter and iterate** -- Without this, the bot is immediately detectable. Every PR arrives within seconds of implementation. Every feedback response is instant. This is the single highest-risk gap.

2. **Populate `recent_traces` in the main loop** -- The fast diagnostic receives an empty list every cycle, meaning circuit breakers never fire. The bot can loop on the same repo forever.

3. **Set `github_username` in health check** -- Fork, push, and duplicate detection all break with an empty username. Add a startup check that halts if `settings.github_username` is empty.

4. **Workspace setup for iteration** -- `_phase_iterate` assumes a workspace exists but never clones the repo. Patch applier will fail on every attempt.

### Priority 2: Core Effectiveness (First merge depends on these)

5. **Implement engage phase** -- Even a minimal version (post one issue comment before contributing) would significantly improve merge odds. The bot currently goes from zero interaction to PR submission.

6. **Wire 12h learn phase into the orchestrator loop** -- Add `last_learn` timer, call deep diagnostic + benchmark on schedule. Without this, the bot never self-corrects at the strategic level.

7. **Call `on_feedback()` after processing iteration feedback** -- Maintainer profiles never get updated because the callback is never invoked.

8. **Write basic tests** -- At least: preflight logic, issue scoring arithmetic, critic JSON parsing, quality gate thresholds. Pure-function tests that run in <1 second.

### Priority 3: Quality Improvements (Improve merge rate)

9. **Wire priority queue into gateway** -- Currently calls bypass the queue entirely. Feedback responses should genuinely preempt implementation calls.

10. **Add question-response path** -- When feedback is classified as `question`, post a comment answering it (call Claude, scrub banned phrases, post via `gh issue comment`). Currently questions are detected but ignored.

11. **Implement notify phase** -- Check for `@mentions` and respond. Low effort, high trust-building.

12. **Add PR description style seed** -- Plan calls for randomized section ordering. Without it, all bot PRs have identical structure.

### Priority 4: Strategic (Sustained merge rate)

13. **Deep analysis: waste ratio, per-repo failure rates, auto-ban underperformers** -- The arithmetic analysis module does not exist yet.

14. **Trending repo discovery** -- Plan Phase 6 item 36. Discover repos with recent activity spikes.

15. **Graceful shutdown with signal handlers** -- SIGTERM cleanup, state flush, connection close.

16. **v3 state migration** -- If any v3 data (outcomes, lessons) should carry forward.

---

## 5. Acceptance Criteria for "v4 is Ready for Production"

### Must-Have (Gate)

- [ ] Humanizer delays are wired and verified: PR creation waits 15-45 min, feedback response waits 30m-4h
- [ ] `recent_traces` buffer is populated from pipeline results so fast_diagnostic has data
- [ ] `github_username` validated at startup; bot halts if empty
- [ ] Iteration workspace is properly cloned before patch_applier runs
- [ ] At least one end-to-end dry run completes: discovery finds repos, scores issues, pops from queue, passes preflight, calls Claude (or mock), quality gates run, outcome recorded
- [ ] No Python import errors when running `python -m osbot`
- [ ] Docker image builds and starts without error
- [ ] Health check passes (claude, gh, state_dir, memory.db)

### Should-Have (Week 1)

- [ ] Engage phase posts at least one issue comment before first PR submission
- [ ] 12h learn phase triggers on schedule
- [ ] Basic test suite: >=10 tests covering scoring, preflight, critic parsing, quality gates
- [ ] Bot runs for 24h in dry-run mode without crash or loop
- [ ] First real PR submitted to an external repo (outcome: submitted, not necessarily merged)

### Nice-to-Have (Week 2+)

- [ ] Priority queue actually gates Claude calls
- [ ] Question feedback gets a response comment
- [ ] Notify phase handles @mentions
- [ ] PR description style variation
- [ ] Deep diagnostic computes waste ratio
- [ ] Contributor benchmarking runs on schedule
- [ ] >=50% test coverage on pipeline and scoring modules

---

## 6. Summary

**Overall completion: ~75%.** The core pipeline (discover -> contribute -> iterate) is structurally complete with all Claude calls wired. The token management system is fully implemented across all 4 layers. The state layer with SQLite and temporal facts is production-quality.

**The critical gaps are operational, not architectural.** The code exists for humanizer delays, diagnostics, and learning -- but these components are not wired into the main loop. The bot would currently submit PRs with zero delay and run diagnostics on an empty trace buffer, making it both detectable and unable to self-correct.

**Estimated effort to reach "Must-Have" gate:** 1-2 days of wiring work (no new modules needed). The code is written; it just needs to be connected.

**Estimated effort to reach "Should-Have":** Additional 3-4 days for engage phase, learn scheduling, and basic tests.

**Risk to first merge:** Moderate. The pipeline quality (constrained prompts, quality gates, critic review) is solid. The main risk is detection due to missing humanizer delays and lack of pre-contribution engagement, which would cause maintainers to identify and reject the bot before the fix quality even matters.
