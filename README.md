# osbot-v4

[![CI](https://github.com/JiwaniZakir/osbot-v4/actions/workflows/ci.yml/badge.svg)](https://github.com/JiwaniZakir/osbot-v4/actions/workflows/ci.yml)
[![CodeQL](https://github.com/JiwaniZakir/osbot-v4/actions/workflows/codeql.yml/badge.svg)](https://github.com/JiwaniZakir/osbot-v4/actions/workflows/codeql.yml)
[![CI Docker](https://github.com/JiwaniZakir/osbot-v4/actions/workflows/ci-docker.yml/badge.svg)](https://github.com/JiwaniZakir/osbot-v4/actions/workflows/ci-docker.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

Autonomous open-source contribution bot. Discovers GitHub issues, implements minimal fixes, submits PRs, iterates on maintainer feedback, and learns from every outcome — behaving like a careful human contributor, not a bot service.

## Architecture

```
Orchestrator (asyncio event loop)
  |
  +-- Token Management (probe -> decay -> decompose -> schedule)
  |
  +-- Discovery Engine (0 Claude calls)
  |     find repos -> score -> find issues -> score -> queue
  |
  +-- Contribution Engine (3 Claude calls per attempt)
  |     preflight -> implement -> quality gates -> critic -> PR -> submit
  |
  +-- Iteration Engine (0-2 Claude calls per PR)
  |     poll PRs -> read feedback -> apply patches -> push
  |
  +-- Learning Engine (0-1 Claude calls per 12h)
  |     diagnostics -> lessons -> reflections -> skill library
  |
  +-- State Layer
        memory.db (SQLite) | state.json | traces.jsonl
```

## Quick Start

```bash
# Clone
git clone https://github.com/JiwaniZakir/osbot-v4.git && cd osbot-v4

# Install dev deps
pip install -e ".[dev]"

# Run tests
pytest tests/

# Run checks
ruff check . && mypy src/osbot
```

## Deploy

Runs in Docker on a Hetzner VPS. See [deployment docs](https://jiwaizakir.github.io/osbot-v4/deployment/).

```bash
cd deploy && docker compose up -d
```

## CI/CD Pipeline

| Workflow | Trigger | Purpose |
|---|---|---|
| CI | Push/PR to main, dev | Lint (ruff + mypy) + test matrix (Python 3.11, 3.12) |
| CI Docker | Push/PR to main, dev | Build Docker image, run tests inside container |
| CodeQL | Push/PR + weekly | Security scanning |
| PR Quality Gate | Every PR | Claude (Opus) evaluates: regression, value, conventions. APPROVE / REFINE / REJECT |
| Claude Interactive | `@claude` comment | Claude responds to mentions on issues and PRs |
| Claude CI Fix | CI failure | Auto-creates fix PR when CI fails |
| Release | Push to main | Semantic versioning + GitHub Release |
| Deploy VPS | Push to main | SSH deploy to Hetzner |
| Docs | Push to main | MkDocs Material -> GitHub Pages |

## Configuration

All config via `OSBOT_*` environment variables. See [configuration docs](https://jiwaizakir.github.io/osbot-v4/configuration/).

## License

[MIT](LICENSE)
