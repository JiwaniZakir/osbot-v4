# Configuration

All configuration is via environment variables with the `OSBOT_` prefix, managed by pydantic-settings. The config object is frozen at startup and never mutated at runtime.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OSBOT_FIVE_HOUR_CEILING` | `0.60` | Max share of 5h rolling token window |
| `OSBOT_SEVEN_DAY_CEILING` | `0.50` | Max share of 7d rolling token window |
| `OSBOT_OPUS_CEILING` | `0.40` | Max share of 7d Opus token window |
| `OSBOT_MAX_WORKERS` | `5` | Maximum concurrent contribution workers |
| `OSBOT_PROBE_INTERVAL_SEC` | `300` | Token probe frequency (5 min) |
| `OSBOT_PLAN_HORIZON_HOURS` | `2.0` | How far ahead the scheduler plans |
| `OSBOT_ESTIMATED_WINDOW_CAPACITY` | `2000000` | Estimated tokens in 5h for Max 20x |
| `OSBOT_TIMEZONE` | `US/Eastern` | User's timezone for pattern model |
| `OSBOT_CYCLE_INTERVAL_SEC` | `600` | Main loop cycle interval (10 min) |
| `OSBOT_CLAUDE_BINARY` | `claude` | Path to Claude CLI binary |
| `OSBOT_STATE_DIR` | `state` | Directory for state.json, memory.db, traces |

## Docker Volumes

| Volume | Mount Point | Purpose |
|---|---|---|
| `claude-credentials` | `/home/botuser/.claude` | OAuth for CLI + usage probe |
| `gh-config` | `/home/botuser/.config/gh` | GitHub CLI auth |
| `osbot-state` | `/opt/osbot/state` | state.json, memory.db, traces, corrections |
