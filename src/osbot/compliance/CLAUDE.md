# osbot.compliance

## Purpose

CLA (Contributor License Agreement) checking and assignment requirement detection. Ensures the bot only submits to repos where it is legally and procedurally allowed to contribute. This is Layer 2 -- depends on state and intel, no Claude calls.

## Key Interfaces

```python
class CLAChecker:
    """Check if CLA is required and whether we've signed it."""

    async def check(self, repo: str) -> CLAResult:
        """Returns: not_required | signed | needs_signing | cannot_sign.
        Checks CLA bot comments, CONTRIBUTING.md mentions, known CLA platforms."""


class AssignmentDetector:
    """Detect if a repo requires issue assignment before PR submission."""

    async def requires_assignment(self, repo: str, issue_number: int) -> bool:
        """Check CONTRIBUTING.md patterns, issue templates, bot comments, label patterns."""
```

Note: The actual assignment flow (claim, await, poll) lives in `osbot.pipeline.assignment`. This module only detects whether assignment is required.

## Dependencies

- `osbot.config` -- known CLA platform URLs
- `osbot.types` -- `CLAResult`
- `osbot.state` -- `MemoryDB` (cache CLA status and assignment requirements in repo_facts)
- `osbot.intel` -- `PolicyReader` (CONTRIBUTING.md parsing), `GraphQLClient` (check for CLA bot comments)
- `osbot.log` -- structured logging

## Internal Structure

- **`cla.py`** -- `CLAChecker`. Checks multiple sources: (1) CONTRIBUTING.md for CLA mentions, (2) known CLA platforms (CLA Assistant, Google CLA, Apache ICLA), (3) PR comment history for CLA bot comments (e.g., "Please sign our CLA"). Results cached in `repo_facts`. If CLA is required and cannot be auto-signed, the repo is excluded from contributions.

- **`assignment.py`** -- `AssignmentDetector`. Detects assignment requirements from: (1) CONTRIBUTING.md text ("please ask to be assigned"), (2) issue template instructions, (3) bot comments on issues ("This issue has been automatically assigned"), (4) label patterns ("help wanted" without "good first issue" often implies assignment). Results cached in `repo_facts` with the same temporal conflict resolution as all other facts.

## How to Test

```python
async def test_cla_detects_cla_assistant(mock_graphql):
    mock_graphql.return_comments([Comment(author="CLAassistant", body="Please sign the CLA")])
    checker = CLAChecker(graphql=mock_graphql, memory=mock_memory)
    result = await checker.check("owner/repo")
    assert result.status == "needs_signing"

async def test_assignment_detects_contributing_requirement(tmp_path):
    (tmp_path / "CONTRIBUTING.md").write_text("Please ask to be assigned before starting work.")
    detector = AssignmentDetector(policy_reader=PolicyReader())
    assert await detector.requires_assignment("owner/repo", 42) is True

async def test_cla_caches_result(memory):
    checker = CLAChecker(memory=memory, ...)
    await checker.check("owner/repo")  # First call - fetches
    await checker.check("owner/repo")  # Second call - cached
    # Assert only one GraphQL call was made
```

- Test CLA detection with mock CLA bot comments.
- Test assignment detection with synthetic CONTRIBUTING.md content via `tmp_path`.
- Test caching behavior to verify results persist in repo_facts.

## Design Decisions

1. **Detection separate from flow.** `compliance.assignment` answers "is assignment required?" `pipeline.assignment` handles the full state machine (claim, await, poll). This separation keeps compliance at Layer 2 (no gateway dependency) and pipeline at Layer 4.

2. **Multiple CLA detection methods.** Different projects use different CLA systems. Checking CONTRIBUTING.md text, known bot usernames, and platform URLs catches the most common cases.

3. **cannot_sign is a permanent exclusion.** If CLA requires organizational approval or a physical signature, the bot cannot comply. The repo is excluded rather than attempting to submit without CLA compliance.

4. **Cached in repo_facts.** CLA requirements and assignment policies rarely change. Caching avoids repeated API calls and keeps the preflight check fast.
