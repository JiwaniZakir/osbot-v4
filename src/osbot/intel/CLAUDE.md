# osbot.intel

## Purpose

Shared intelligence-gathering utilities used by discovery, pipeline, and iteration. Provides GraphQL access, codebase analysis, CONTRIBUTING.md policy parsing, and duplicate PR detection. This is Layer 2 -- it depends on state but not on gateway or any higher layer. No Claude calls; all analysis is rule-based or API-driven.

## Key Interfaces

```python
class GraphQLClient:
    """Shared GraphQL client for GitHub API v4."""

    async def get_issue_details(self, repo: str, number: int) -> IssueDetails:
        """Fetch labels, comments, reactions, timeline events, author association."""

    async def get_pr_comments(self, repo: str, number: int,
                              since: str | None = None) -> list[Comment]:
        """Fetch PR comments, optionally filtered by timestamp."""

    async def get_pr_reviews(self, repo: str, number: int) -> list[Review]:
        """Fetch PR review status (approved, changes_requested, etc.)."""

    async def get_ci_status(self, repo: str, ref: str) -> CIStatus:
        """Fetch CI check runs and their conclusions."""

    async def get_issue_timeline(self, repo: str, number: int) -> list[TimelineEvent]:
        """Fetch timeline events (assigned, labeled, referenced, etc.)."""

    async def batch_issues(self, repo: str, labels: list[str] | None = None,
                           limit: int = 50) -> list[IssueDetails]:
        """Fetch multiple issues in one GraphQL query."""


class CodebaseAnalyzer:
    """Style profiling for implementation context."""

    async def analyze(self, workspace: str) -> StyleNotes:
        """Scan repo for: indent style, import conventions, test framework,
        docstring format, naming conventions. Returns <= 200 tokens of notes."""


class PolicyReader:
    """Parse CONTRIBUTING.md and related files."""

    async def read(self, workspace: str) -> RepoPolicy:
        """Extract: requires_assignment, commit_format, lint_command, test_command,
        pr_template, no_ai_policy, cla_required."""


class DuplicateDetector:
    """Check if a PR already exists for an issue."""

    async def check(self, repo: str, issue_number: int) -> DuplicateResult:
        """Query GraphQL timeline for linked PRs. Also checks our own open PRs."""
```

## Dependencies

- `osbot.config` -- GitHub API settings
- `osbot.types` -- `IssueDetails`, `Comment`, `Review`, `CIStatus`, `TimelineEvent`, `StyleNotes`, `RepoPolicy`, `DuplicateResult`
- `osbot.state` -- `MemoryDB` (cache policy results in repo_facts)
- `osbot.log` -- structured logging

External: `gh` CLI (via `asyncio.create_subprocess_exec` for GraphQL queries).

## Internal Structure

- **`graphql_client.py`** -- `GraphQLClient`. Executes GraphQL queries via `gh api graphql`. Provides typed methods for common queries (issue details, PR comments, CI status, timeline). Handles pagination for large result sets. Batches multiple issue queries into single GraphQL requests where possible. All calls via `asyncio.create_subprocess_exec` (never `subprocess.run`).

- **`codebase_analyzer.py`** -- `CodebaseAnalyzer`. Scans the cloned workspace (no Claude) to detect: indent style (tabs vs spaces, width), import ordering, test framework (pytest, unittest, nose), docstring format (Google, NumPy, Sphinx), naming conventions (snake_case, camelCase). Output is a `StyleNotes` string of <= 200 tokens, injected into the implementation prompt so Claude follows the repo's conventions.

- **`policy_reader.py`** -- `PolicyReader`. Reads `CONTRIBUTING.md`, `CONTRIBUTING.rst`, `.github/CONTRIBUTING.md`, and `PULL_REQUEST_TEMPLATE.md`. Parses for: assignment requirements ("please ask to be assigned"), commit message format ("Conventional Commits"), test/lint commands ("run `pytest`"), CLA requirements, and no-AI policies ("we do not accept AI-generated contributions"). Results cached in `memory.db.repo_facts`.

- **`duplicate_detector.py`** -- `DuplicateDetector`. Queries the issue's GraphQL timeline for `CrossReferencedEvent` linking to PRs. Also queries our own open PRs (by bot's GitHub username) to prevent submitting multiple PRs for the same issue. Returns `DuplicateResult` with `is_duplicate` flag and the existing PR URL if found.

## How to Test

```python
async def test_policy_reader_detects_assignment(tmp_path):
    (tmp_path / "CONTRIBUTING.md").write_text("Please ask to be assigned before working on an issue.")
    reader = PolicyReader()
    policy = await reader.read(str(tmp_path))
    assert policy.requires_assignment is True

async def test_policy_reader_detects_no_ai(tmp_path):
    (tmp_path / "CONTRIBUTING.md").write_text("We do not accept AI-generated pull requests.")
    reader = PolicyReader()
    policy = await reader.read(str(tmp_path))
    assert policy.no_ai_policy is True

async def test_codebase_analyzer_detects_pytest(tmp_path):
    (tmp_path / "tests" / "test_foo.py").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest]")
    analyzer = CodebaseAnalyzer()
    notes = await analyzer.analyze(str(tmp_path))
    assert "pytest" in notes.text

async def test_duplicate_detector_finds_own_pr(mock_graphql):
    mock_graphql.return_timeline([TimelineEvent(type="cross-referenced", pr_author="JiwaniZakir")])
    detector = DuplicateDetector(graphql=mock_graphql, bot_username="JiwaniZakir")
    result = await detector.check("owner/repo", 42)
    assert result.is_duplicate
```

- `PolicyReader` and `CodebaseAnalyzer` can be tested with real files via `tmp_path`.
- `GraphQLClient` and `DuplicateDetector` need mocked `gh` CLI or mock GraphQL responses.
- `StyleNotes` output should be tested for token count (<= 200).

## Design Decisions

1. **No Claude calls.** All analysis is rule-based (regex, file scanning, pattern matching). This keeps intel at Layer 2 and allows it to be called freely without token budget concerns.

2. **GraphQL over REST.** A single GraphQL query can fetch issue details, comments, timeline, and reactions in one round trip. REST would require 4+ separate requests. This is critical for the monitor phase which polls all open PRs every cycle.

3. **Style notes capped at 200 tokens.** The implementation prompt has a token budget. Injecting a full style guide would crowd out the actual task. 200 tokens is enough for "uses pytest, 4-space indent, Google docstrings, snake_case."

4. **Policy caching in repo_facts.** CONTRIBUTING.md rarely changes. Caching parsed results as temporal facts (with `valid_from`/`valid_until`) means the bot only re-parses when explicitly invalidated or on discovery refresh.

5. **Duplicate detection includes self-check.** v3 had a bug where the bot submitted 7 PRs for the same issue. Checking our own open PRs (by bot username) prevents this class of bug entirely.
