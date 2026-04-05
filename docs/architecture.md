# Architecture

## System Diagram

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
|  |  memory.db (SQLite)  |  state.json  |  traces.jsonl          |  |
|  +---------------------------------------------------------------+  |
+---------------------------------------------------------------------+
```

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

LEARN (every 12h + event-triggered, 0-1 Claude calls)
  Fast diagnostic (every cycle, 0 Claude): loop detection, timeout patterns
  Deep diagnostic (12h): waste ratio, per-repo failure, ban underperformers
  Event-triggered: on merge -> extract "what worked", on rejection -> lesson
```

## Module Map

| Package | Purpose | Calls Claude? |
|---|---|---|
| `state` | AsyncIO-safe state + SQLite memory | No |
| `gateway` | Agent SDK wrapper, priority queue | Yes (all calls route here) |
| `tokens` | 4-layer token management | No |
| `discovery` | Find repos, score repos, find+score issues | No |
| `pipeline` | Preflight, implement, quality gates, critic, PR, submit | Yes (3 calls) |
| `iteration` | Monitor open PRs, read feedback, apply patches | Yes (0-2 calls) |
| `intel` | GraphQL client, codebase analyzer, policy reader | No |
| `safety` | Anti-spam, circuit breakers, repo bans | No |
| `compliance` | CLA checking, assignment detection | No |
| `comms` | Comment generation, 40+ banned AI phrases | No |
| `timing` | Humanizer delays | No |
| `learning` | Self-diagnostics, lesson extraction | Yes (0-1/12h) |
| `orchestrator` | asyncio event loop, phase scheduling | No (calls engines) |
