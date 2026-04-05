# osbot.pipeline

## Purpose

The contribution pipeline: take a scored issue from the queue and produce a submitted PR. This is where all 3 core Claude calls happen (implement, critic, PR description). It also handles preflight validation, assignment flow, quality gates, and submission mechanics. This is Layer 4 -- it orchestrates Layer 2 modules (gateway, intel, comms, compliance) to produce a contribution.

## Key Interfaces

```python
class ContributionPipeline:
    """End-to-end: issue -> submitted PR."""

    async def run(self, issue: Issue) -> PipelineResult:
        """Execute full pipeline. Returns result with outcome and trace."""


class Preflight:
    """All validation gates before Claude is called. No Claude calls."""

    async def check(self, issue: Issue) -> PreflightResult:
        """Returns pass/fail with reason. Checks bans, domain, duplicates,
        issue state, CLA, maintainer activity, assignment status."""


class AssignmentFlow:
    """State machine for repos that require assignment."""

    async def claim(self, issue: Issue) -> ClaimResult:
        """Post a claim comment on the issue. Returns awaiting_assignment state."""

    async def poll(self, issue: Issue) -> AssignmentStatus:
        """Check if we were assigned. Returns assigned | assigned_other | timeout."""


class Implementer:
    """Call #1: Minimal-scope fix."""

    async def implement(self, issue: Issue, workspace: str) -> ImplementResult:
        """Clone repo, invoke Claude with constrained prompt, return diff + tool trace."""


class QualityGates:
    """Post-implementation validation. No Claude calls."""

    async def check(self, workspace: str, diff: str) -> QualityResult:
        """Diff <= 50 lines, <= 3 files, test touched, no reformats, lint, tests, commit msg."""


class Critic:
    """Call #2: MAR-style review with actor tool trace."""

    async def review(self, issue: Issue, diff: str, tool_trace: list[dict]) -> CriticResult:
        """Returns APPROVE or REJECT. HARD GATE: REJECT = permanently rejected."""


class PRWriter:
    """Call #3: PR description generation."""

    async def write(self, issue: Issue, diff: str) -> PRDescription:
        """Generate body with Closes #N, file paths, function names, before/after, test output."""


class Submitter:
    """Fork, clone, push, create PR."""

    async def submit(self, issue: Issue, workspace: str, description: PRDescription) -> SubmitResult:
        """Fork repo, push branch, gh pr create. Humanizer delay applied before creation."""
```

## Dependencies

- `osbot.config` -- timeouts, diff limits, file limits
- `osbot.types` -- `Issue`, `PipelineResult`, `AgentResult`, `PreflightResult`, etc.
- `osbot.state` -- `MemoryDB` (check bans, record outcomes), `BotState` (update active work)
- `osbot.gateway` -- `GatewayProtocol` (3 Claude calls: implement, critic, pr_writer)
- `osbot.intel` -- `GraphQLClient` (issue state), `CodebaseAnalyzer` (style notes), `PolicyReader`, `DuplicateDetector`
- `osbot.comms` -- `CommentGenerator` (claim comments, banned phrase filtering)
- `osbot.compliance` -- `CLAChecker`, assignment detection
- `osbot.timing` -- `Humanizer` (PR creation delay)
- `osbot.log` -- structured logging

## Internal Structure

- **`preflight.py`** -- `Preflight`. Runs all checks before any Claude call is made. Order: repo not banned (memory.db) -> in domain (language + topic) -> not previously rejected (outcomes table) -> no duplicate PR (GraphQL timeline) -> issue still open -> CLA OK -> maintainer active (last commit < 30d) -> assignment status. Any failure short-circuits with a reason string. No artificial caps, no weekly limits, no cooldowns -- token management is the only throughput gate.

- **`assignment.py`** -- `AssignmentFlow`. Detects assignment requirements from CONTRIBUTING.md and issue patterns. Posts a natural-language claim comment (via comms, with banned phrases filtered). Tracks state: `awaiting_assignment` -> poll each cycle via GraphQL (no Claude, no worker slot consumed) -> `assigned` (proceed) | `assigned_other` (reject) | 72h timeout (reject).

- **`implementer.py`** -- `Implementer`. Call #1. Clones the repo into a temp workspace. Builds a constrained prompt with: repo info, branch, issue description, test command, style notes (200 tokens max from codebase analyzer). The prompt has explicit TASK and FORBIDDEN sections. FORBIDDEN: unrelated files, unnecessary imports, whole-file reformats, new abstractions, docstrings on unchanged code. Model: sonnet. Timeout: 180s.

- **`quality_gates.py`** -- `QualityGates`. Post-implementation checks with no Claude calls. Validates: diff <= 50 lines (CCA data: 76-80% merge rate at 1-50 lines), <= 3 files changed, test file touched if repo has tests/, no whole-file reformats detected, linter passes, test suite passes, commit message 50-72 chars. Each check returns pass/fail with detail.

- **`critic.py`** -- `Critic`. Call #2. Receives issue title, the implementer's tool call trace, and the diff. Uses MAR-style (Multi-Agent Review) prompting. Output: strict JSON `{"verdict": "APPROVE"|"REJECT", "reason": "..."}`. HARD GATE: REJECT = permanently rejected, no retry, no "accept after N failed iterations." Model: opus (sonnet fallback if Opus budget tight, controlled by `balancer.should_prefer_sonnet`). Timeout: 120s.

- **`pr_writer.py`** -- `PRWriter`. Call #3. Generates PR description. `Closes #{number}` is a template literal (not generated by Claude). Body must include: >= 1 file path, >= 1 function name, before/after comparison, test output snippet. All output filtered through comms for banned AI phrases. Specificity validation: at least 2 concrete code references required. Model: sonnet. Timeout: 60s.

- **`submitter.py`** -- `Submitter`. Mechanics of PR creation. Forks the repo (if not already forked), clones to workspace, applies the diff, commits, pushes to a feature branch. Then `gh pr create` with the generated description. Humanizer delay (15-45 min) is applied before `gh pr create` to avoid appearing automated. All git/gh calls via `asyncio.create_subprocess_exec`.

## How to Test

```python
async def test_preflight_rejects_banned_repo(memory):
    await memory.ban_repo("owner/repo", "loop", 7, "fast_diagnostic")
    pf = Preflight(memory=memory, ...)
    result = await pf.check(Issue(repo="owner/repo", number=1, score=8.0))
    assert not result.passed
    assert "banned" in result.reason

async def test_quality_gates_reject_large_diff():
    qg = QualityGates()
    diff = "+" * 100 + "\n" * 60  # > 50 lines
    result = await qg.check("/tmp/workspace", diff)
    assert not result.passed
    assert "50 lines" in result.reason

async def test_critic_hard_gate(mock_gateway):
    mock_gateway.responses["critic"] = AgentResult(
        success=True, text='{"verdict": "REJECT", "reason": "scope creep"}', ...)
    critic = Critic(gateway=mock_gateway)
    result = await critic.review(issue, diff, tool_trace)
    assert result.verdict == "REJECT"

async def test_full_pipeline_happy_path(mock_gateway, memory):
    pipeline = ContributionPipeline(gateway=mock_gateway, memory=memory, ...)
    result = await pipeline.run(issue)
    assert result.outcome == "submitted"
    assert len(mock_gateway.calls) == 3  # implement, critic, pr_writer
```

- Mock gateway returns canned `AgentResult` for each phase.
- Mock `asyncio.create_subprocess_exec` for git/gh commands.
- Test each component independently, then test `ContributionPipeline` integration.

## Design Decisions

1. **3 Claude calls, not 23.** v3 averaged 23.6 calls per submission. The pipeline is structured so each call has a distinct purpose with no redundancy. Preflight and quality gates are free.

2. **Critic is a HARD gate.** No "accept after 2 failed iterations." If the critic rejects, the contribution is permanently abandoned. This prevents the bot from submitting low-quality work that burns maintainer goodwill.

3. **Closes #N is a template literal.** v3 sometimes generated incorrect issue references. The issue number is injected by code, not by Claude.

4. **Implementation prompt has FORBIDDEN section.** Explicitly telling Claude what NOT to do (reformats, new abstractions, unrelated files) is more effective than hoping the TASK section is sufficient.

5. **Quality gates are free.** Diff size, file count, lint, and tests are all checked without Claude. This catches obvious problems before spending tokens on the critic.

6. **Assignment flow is a state machine.** Repos that require assignment get a claim comment, then the bot polls via GraphQL (no Claude, no worker slot consumed) until assignment is granted, denied, or timed out. This is free and respectful.
