# Contributing

## Setup

```bash
git clone https://github.com/JiwaniZakir/osbot-v4.git
cd osbot-v4
pip install -e ".[dev]"
```

## Development Workflow

1. Create a feature branch from `dev`: `git checkout -b feat/my-feature dev`
2. Make changes following the conventions below
3. Run checks: `ruff check . && ruff format --check . && mypy src/osbot && pytest tests/`
4. Open a PR targeting `dev` with a conventional commit title (e.g., `feat: add ...`)
5. The Claude Quality Gate will auto-review your PR

## Conventions

- **asyncio everywhere** — no threads, no `subprocess.run`, no blocking I/O
- **async subprocess** — all `gh`/`git` calls use `asyncio.create_subprocess_exec`
- **structlog** for logging — never `print()` or stdlib `logging`
- **Type hints** on every function signature; `mypy --strict` must pass
- **pydantic models** for data crossing module boundaries
- **No upward imports** — Layer N never imports from Layer N+1
- **Ruff** for linting, target line length 120

## Testing

```bash
pytest tests/                              # all tests
pytest tests/test_state.py                 # specific file
pytest --cov=osbot --cov-report=term-missing  # with coverage
```

- Gateway is always mocked; tests never call real Claude/GitHub
- SQLite uses in-memory (`:memory:`) databases
- `pytest-asyncio` with `asyncio_mode = "auto"`
# Coverage target: raise to 35% by next Friday audit
