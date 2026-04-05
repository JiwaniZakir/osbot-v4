## Summary
<!-- What does this PR change? Be specific about bot behavior changes. -->

## Type of change
- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change (requires config update)
- [ ] Refactor
- [ ] CI/infra change

## Testing
- [ ] Unit tests added/updated
- [ ] `pytest tests/` passes locally
- [ ] `ruff check . && mypy src/osbot` passes

## Checklist
- [ ] Code follows asyncio conventions (no threads, no subprocess.run)
- [ ] structlog used for all logging (no print/stdlib logging)
- [ ] Type annotations on all function signatures
- [ ] No upward layer imports (Layer N never imports Layer N+1)
- [ ] No hardcoded tokens or secrets
