# osbot v4

An autonomous open-source contribution bot that runs 24/7 on a Hetzner VPS, using Claude via the Agent SDK (Max 20x subscription, shared with user's personal usage). It discovers repos, scores issues, implements minimal fixes, responds to maintainer feedback, and learns from outcomes -- behaving like a careful human contributor, not a bot service.

## Architecture

```
+---------------------------------------------------------------------+
|                         ORCHESTRATOR                                |
|                   asyncio event loop, single process                |
|                                                                     |
|  +-------------------------------------------------------------+   |
|  |                  TOKEN MANAGEMENT SYSTEM                     |   |
|  |  L1: Utilization Probe (OAuth /api/oauth/usage, every 5m)   |   |
|  |  L2: Window Decay Model (our tokens x timestamp -> decay)   |   |
|  |  L3: Usage Decomposer (total delta - bot = user)            |   |
|  |  L4: Predictive Scheduler (weekly pattern -> worker plan)   |   |
|  |  Output: workers_this_cycle (1-5), should_prefer_sonnet     |   |
|  +----------------------------+--------------------------------+   |
|                               |                                     |
|  +-------------+  +-----------v-----------+  +------------------+  |
|  | DISCOVERY   |  |    CONTRIBUTION       |  |    LEARNING      |  |
|  | ENGINE      |  |    ENGINE             |  |    ENGINE        |  |
|  |             |  |                       |  |                  |  |
|  | find repos  |  | preflight             |  | fast diagnostic  |  |
|  | score       |  | [assignment flow]     |  | deep analysis    |  |
|  | filter      |  | implement (sonnet)    |  | lesson extract   |  |
|  | queue       |  | quality gates         |  | pattern model    |  |
|  |             |  | critic (opus/sonnet)  |  | repo signals     |  |
|  | 0 Claude    |  | PR description        |  | corrections      |  |
|  | calls       |  | submit                |  |                  |  |
|  |             |  | iterate on feedback   |  | 0-1 Claude/12h   |  |
|  +------+------+  +-----------+-----------+  +--------+---------+  |
|         |                     |                        |            |
|  +------v---------------------v------------------------v--------+  |
|  |                      STATE LAYER                              |  |
|  |  memory.db (SQLite)                                           |  |
|  |  +-- repo_facts (temporal, conflict-aware)                    |  |
|  |  +-- repo_signals (external merge rate, response time, etc)   |  |
|  |  +-- repo_bans (circuit breakers)                             |  |
|  |  +-- outcomes (every PR attempt with result + tokens used)    |  |
|  |  +-- maintainer_profiles                                      |  |
|  |  +-- usage_snapshots (probe history, 7 days)                  |  |
|  |  +-- usage_deltas (bot vs user decomposition)                 |  |
|  |  +-- user_pattern (weekly heatmap, learned)                   |  |
|  |                                                               |  |
|  |  state.json (issue queue, active work)                        |  |
|  |  traces.jsonl (every attempt outcome, append-only)            |  |
|  |  corrections.jsonl (self-diagnostic actions, audit trail)     |  |
|  +---------------------------------------------------------------+  |
|                               |                                     |
|                  +------------v------------+                        |
|                  |     CLAUDE GATEWAY      |                        |
|                  |  Agent SDK + CLI OAuth   |                        |
|                  |  Priority queue          |                        |
|                  +------------+------------+                        |
|                               |                                     |
|                  +------------v------------+                        |
|                  |  GitHub (gh CLI+GraphQL) |                        |
|                  +-------------------------+                        |
+---------------------------------------------------------------------+
```

## Module Map

| Package | Purpose | Calls Claude? | Key Dependencies |
|---|---|---|---|
| `state` | AsyncIO-safe state + SQLite memory | No | (none -- Layer 0) |
| `gateway` | Agent SDK wrapper, priority queue, result type | Yes (all calls route here) | state |
| `tokens` | 4-layer token management (probe, decay, decompose, schedule) | No | state, gateway (records usage) |
| `discovery` | Find repos, score repos, enrich signals, find+score issues | No | state, intel |
| `pipeline` | Preflight, assignment, implement, quality gates, critic, PR writer, submit | Yes (3 calls) | state, gateway, intel, comms, compliance, timing |
| `iteration` | Monitor open PRs, read feedback, apply patches | Yes (2 calls) | state, gateway, intel, comms, timing |
| `intel` | GraphQL client, codebase analyzer, policy reader, duplicate detector | No | state |
| `safety` | Anti-spam, circuit breakers, repo bans | No | state |
| `compliance` | CLA checking, assignment detection | No | state, intel |
| `comms` | Comment generation, 40+ banned AI phrases, voice consistency | No | state |
| `timing` | Humanizer delays (15-45 min PR, 30m-4h feedback) | No | (none) |
| `learning` | Self-diagnostics, lesson extraction, contributor benchmarking | Yes (0-1/12h) | state, gateway |
| `orchestrator` | asyncio event loop, phase scheduling, health check | No (calls engines) | all packages |

## Dependency Layers

```
Layer 0:  timing, config, types, log
Layer 1:  state (depends only on Layer 0)
Layer 2:  gateway, intel, safety, comms, compliance (depend on state)
Layer 3:  tokens (depends on state + gateway)
Layer 4:  discovery, pipeline, iteration, learning (depend on Layers 0-3)
Layer 5:  orchestrator (depends on everything)
```

No upward imports allowed. A module in Layer N never imports from Layer N+1.

## Configuration

All configuration via environment variables with `OSBOT_` prefix, managed by pydantic-settings.

```
OSBOT_FIVE_HOUR_CEILING=0.60        # Max share of 5h rolling window
OSBOT_SEVEN_DAY_CEILING=0.50        # Max share of 7d rolling window
OSBOT_OPUS_CEILING=0.40             # Max share of 7d Opus window
OSBOT_MAX_WORKERS=5                 # Maximum concurrent contribution workers
OSBOT_PROBE_INTERVAL_SEC=300        # Token probe frequency (5 min)
OSBOT_PLAN_HORIZON_HOURS=2.0        # How far ahead the scheduler plans
OSBOT_ESTIMATED_WINDOW_CAPACITY=2000000  # Estimated tokens in 5h for Max 20x
OSBOT_TIMEZONE=US/Eastern           # User's timezone for pattern model
OSBOT_CYCLE_INTERVAL_SEC=600        # Main loop cycle (10 min)
OSBOT_CLAUDE_BINARY=claude          # Path to Claude CLI binary
OSBOT_STATE_DIR=state               # Directory for state.json, memory.db, traces
```

Config is a frozen pydantic BaseSettings object. Never mutated at runtime.

## Data Flow

```
DISCOVER (every 30 min, 0 Claude calls)
  GitHub search -> repo candidates -> signal enrichment -> score -> active pool
  Issue search -> GraphQL enrichment -> score -> issue queue in state.json

CONTRIBUTE (every cycle, 3 Claude calls per attempt)
  Pop issue from queue -> preflight gates (no Claude)
  -> [assignment flow if needed: claim comment -> await -> poll]
  -> Call #1: implement (sonnet, 180s) -> quality gates (no Claude)
  -> Call #2: critic (opus, 120s) -> HARD GATE
  -> Call #3: PR description (sonnet, 60s) -> submit (fork, push, gh pr create)

ITERATE (every cycle, 0-2 Claude calls per PR)
  Poll open PRs via GraphQL -> detect new comments/reviews/CI status
  -> Call #4: feedback reader (sonnet) -> classify action type
  -> Call #5: patch applier (sonnet) -> apply changes, quality gates, push
  Safety: max 3 rounds, no size growth >120%, stop on merge conflicts

LEARN (every 12h + event-triggered, 0-1 Claude calls)
  Fast diagnostic (every cycle, 0 Claude): loop detection, timeout patterns, dead cycles
  Deep diagnostic (12h): waste ratio, per-repo failure, ban underperformers
  Event-triggered: on merge -> extract "what worked", on 3rd rejection -> negative lesson
```

## How to Test

```bash
# Run all tests from project root
pytest

# Run a specific package's tests
pytest tests/state/
pytest tests/gateway/

# Run with coverage
pytest --cov=osbot --cov-report=term-missing
```

**Testing strategy:**
- Gateway is always mocked. Tests never call the real Claude CLI or Agent SDK.
- SQLite uses in-memory databases (`":memory:"`) in tests -- no disk I/O.
- `conftest.py` provides shared fixtures: mock gateway, in-memory state, sample repos/issues.
- Use `pytest-asyncio` with `asyncio_mode = "auto"` -- all `async def test_*` functions run automatically.
- `gh` and `git` subprocess calls are mocked via `asyncio.create_subprocess_exec` patches.

## Conventions

- **asyncio everywhere.** No threads, no `ThreadPoolExecutor`. All I/O is async.
- **async subprocess.** All `gh` and `git` calls use `asyncio.create_subprocess_exec`. Never `subprocess.Popen` or `subprocess.run`.
- **structlog** for all logging. Import from `osbot.log`. Never use `print()` or stdlib `logging`.
- **Type hints** on every function signature. `mypy --strict` must pass.
- **Protocols** for dependency injection (e.g., `GatewayProtocol`). No concrete class imports across layer boundaries.
- **pydantic** models for all data structures that cross module boundaries.
- **No hardcoded repo names.** Discovery is fully dynamic.
- **No artificial limits.** Token management is the only throughput gate.
- Python 3.12+. Ruff for linting (`ruff check`), target line length 120.

## Deploy

Docker on Hetzner CX22 VPS (162.55.191.181).

```bash
# Quick deploy: scp changed files + rebuild
scp src/osbot/pipeline/implementer.py aegis-ext:/opt/osbot/src/osbot/pipeline/
ssh aegis-ext 'cd /opt/osbot/deploy && docker compose down && docker compose build --no-cache && docker compose up -d'

# Full deploy
ssh aegis-ext && cd /opt/osbot/deploy && ./deploy.sh
```

Volumes:
```yaml
volumes:
  - claude-credentials:/home/botuser/.claude    # OAuth for CLI + usage probe
  - gh-config:/home/botuser/.config/gh          # GitHub auth
  - osbot-state:/opt/osbot/state                # state.json, memory.db, traces, corrections
```

## File Layout

```
osbot-v4/
+-- CLAUDE.md                     # This file
+-- pyproject.toml                # hatchling build, deps, tool config
+-- src/osbot/
|   +-- __init__.py
|   +-- __main__.py               # Entry point: main_sync()
|   +-- config.py                 # Pydantic BaseSettings, OSBOT_ prefix
|   +-- types.py                  # Shared pydantic models (AgentResult, Issue, Repo, etc.)
|   +-- log.py                    # structlog configuration
|   +-- state/                    # Layer 1: BotState + SQLite memory
|   +-- gateway/                  # Layer 2: ClaudeGateway (Agent SDK)
|   +-- tokens/                   # Layer 3: 4-layer token management
|   +-- discovery/                # Layer 4: Repo+issue discovery (0 Claude)
|   +-- pipeline/                 # Layer 4: Contribution pipeline (3 Claude)
|   +-- iteration/                # Layer 4: PR feedback loop (0-2 Claude)
|   +-- intel/                    # Layer 2: GraphQL, codebase, policy, dedup
|   +-- safety/                   # Layer 2: Anti-spam, circuit breakers
|   +-- compliance/               # Layer 2: CLA, assignment
|   +-- comms/                    # Layer 2: Comment generation
|   +-- timing/                   # Layer 0: Humanizer delays
|   +-- learning/                 # Layer 4: Diagnostics, lessons
|   +-- orchestrator/             # Layer 5: Main loop
+-- tests/                        # Mirrors src/osbot/ structure
+-- state/                        # Runtime: state.json, memory.db, traces.jsonl
+-- deploy/                       # Dockerfile, docker-compose.yml
```
