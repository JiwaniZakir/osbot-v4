# osbot-v4

Autonomous open-source contribution bot that runs 24/7, discovering GitHub issues, implementing minimal fixes, submitting PRs, iterating on maintainer feedback, and learning from every outcome.

## How It Works

1. **Discover** — searches GitHub for repos and issues matching contribution criteria (zero Claude calls)
2. **Contribute** — implements fixes using Claude, runs quality gates, submits PRs (3 Claude calls per attempt)
3. **Iterate** — monitors open PRs, reads maintainer feedback, applies patches (0-2 Claude calls)
4. **Learn** — runs diagnostics, extracts lessons from outcomes, adjusts scoring (0-1 Claude call per 12h)

## Key Design Principles

- **Behaves like a careful human contributor**, not a bot service
- **Token-aware** — 4-layer token management shares a Max 20x subscription with the user
- **Self-improving** — reflexion system, skill library, meta-lessons, and prompt variant A/B testing
- **Safety-first** — anti-spam guards, circuit breakers, repo bans, CLA checking, assignment detection

## Quick Links

- [Architecture](architecture.md) — system diagram, dependency layers, data flow
- [Configuration](configuration.md) — all `OSBOT_*` environment variables
- [Deployment](deployment.md) — Docker on Hetzner VPS
- [Contributing](contributing.md) — how to work on the bot itself
