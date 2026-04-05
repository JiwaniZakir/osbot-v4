"""Shared types for osbot v4.

Every module depends on this file. It has zero internal dependencies.
All dataclasses use slots=True. Frozen where the object is immutable after creation.
Full type hints throughout.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Outcome(enum.Enum):
    """Result of a PR submission."""

    MERGED = "merged"
    REJECTED = "rejected"
    IGNORED = "ignored"
    ITERATED_MERGED = "iterated_merged"
    STUCK = "stuck"
    SUBMITTED = "submitted"


class IssueStatus(enum.Enum):
    """Lifecycle of an issue in our queue."""

    QUEUED = "queued"
    AWAITING_ASSIGNMENT = "awaiting_assignment"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    DONE = "done"
    REJECTED = "rejected"
    EXPIRED = "expired"


class FeedbackType(enum.Enum):
    """Classification of maintainer feedback on our PR."""

    REQUEST_CHANGES = "request_changes"
    STYLE_FEEDBACK = "style_feedback"
    QUESTION = "question"
    APPROVAL_PENDING_MINOR = "approval_pending_minor"
    REJECTION_WITH_REASON = "rejection_with_reason"
    CI_FAILURE = "ci_failure"


class CriticVerdict(enum.Enum):
    """Critic (Call #2) output."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"


class Phase(enum.Enum):
    """Orchestrator phases."""

    HEALTH_CHECK = "health_check"
    DISCOVER = "discover"
    CONTRIBUTE = "contribute"
    ITERATE = "iterate"
    REVIEW = "review"
    ENGAGE = "engage"
    MONITOR = "monitor"
    FAST_DIAG = "fast_diag"
    LEARN = "learn"
    NOTIFY = "notify"


class Priority(enum.IntEnum):
    """Claude gateway queue priority (lower number = higher priority)."""

    FEEDBACK_RESPONSE = 0
    CRITIC = 1
    IMPLEMENTER = 2
    PATCH_APPLIER = 3
    PR_WRITER = 4
    CLAIM_COMMENT = 5
    LESSON = 6
    DIAGNOSTIC = 7


# ---------------------------------------------------------------------------
# Dataclasses — Frozen (immutable after creation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoMeta:
    """Metadata for a repository in the active pool."""

    owner: str
    name: str
    language: str
    stars: int
    description: str = ""
    topics: list[str] = field(default_factory=list)
    has_contributing: bool = False
    requires_assignment: bool = False
    has_ai_policy: bool = False
    ci_enabled: bool = False
    external_merge_rate: float = 0.0        # 0.0 - 1.0
    avg_response_hours: float = 0.0
    close_completion_rate: float = 0.0
    score: float = 0.0

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"
    last_push_at: str = ""


@dataclass(frozen=True, slots=True)
class ScoredIssue:
    """An issue scored and ready for the queue."""

    repo: str                               # "owner/name"
    number: int
    title: str
    body: str = ""
    labels: list[str] = field(default_factory=list)
    url: str = ""
    score: float = 0.0
    maintainer_confirmed: bool = False
    has_error_trace: bool = False
    has_code_block: bool = False
    requires_assignment: bool = False
    created_at: str = ""
    updated_at: str = ""
    comment_count: int = 0
    reaction_count: int = 0


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Result from a Claude Agent SDK invocation."""

    success: bool
    text: str
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    tokens_used: int = 0
    model: str = ""


@dataclass(frozen=True, slots=True)
class CLIResult:
    """Result from a gh/git CLI invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True, slots=True)
class CriticResult:
    """Output of the critic (Call #2)."""

    verdict: CriticVerdict
    reasoning: str
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class QualityGateResult:
    """Output of the quality gates check (no Claude)."""

    passed: bool
    failures: list[str] = field(default_factory=list)
    diff_lines: int = 0
    files_changed: int = 0
    tests_touched: bool = False
    lint_passed: bool = False
    tests_passed: bool = False


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """End-to-end result of the contribution pipeline for one issue."""

    repo: str
    issue_number: int
    outcome: Outcome
    pr_number: int | None = None
    pr_url: str | None = None
    failure_reason: str | None = None
    failure_phase: str | None = None
    tokens_used: int = 0
    claude_calls: int = 0
    duration_sec: float = 0.0


@dataclass(frozen=True, slots=True)
class Trace:
    """Single append-only trace entry (traces.jsonl)."""

    ts: str                                 # ISO 8601
    repo: str
    issue_number: int
    phase: str
    outcome: str                            # success, rejected, timeout, error, skipped
    failure_reason: str | None = None
    tokens_used: int = 0
    claude_calls: int = 0
    duration_sec: float = 0.0
    pr_number: int | None = None


@dataclass(frozen=True, slots=True)
class Correction:
    """Self-diagnostic correction (corrections.jsonl)."""

    ts: str                                 # ISO 8601
    type: str                               # ban_repo, alert, score_adjust, force_discovery
    repo: str = ""
    days: int = 0
    reason: str = ""
    severity: str = ""                      # low, medium, high (for alerts)
    message: str = ""


@dataclass(frozen=True, slots=True)
class OpenPR:
    """Tracking state for a PR we submitted."""

    repo: str
    issue_number: int
    pr_number: int
    url: str
    branch: str
    submitted_at: str
    last_checked_at: str = ""
    iteration_count: int = 0
    status: str = "open"                    # open, merged, closed


@dataclass(frozen=True, slots=True)
class FeedbackAction:
    """A parsed action item from maintainer feedback."""

    feedback_type: FeedbackType
    summary: str
    file_path: str | None = None
    line_number: int | None = None
    details: str = ""


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    """Result of classifying feedback on a PR (Call #4)."""

    feedback_type: FeedbackType
    actions: list[FeedbackAction] = field(default_factory=list)
    should_respond: bool = True
    should_patch: bool = False
    is_terminal: bool = False               # True if rejection


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """A single probe reading from the OAuth usage endpoint."""

    ts: str                                 # ISO 8601
    five_hour: float                        # 0.0 - 1.0 utilization
    seven_day: float
    opus_weekly: float
    sonnet_weekly: float


@dataclass(frozen=True, slots=True)
class UsageDelta:
    """Decomposed usage between two probe readings."""

    period_start: str
    period_end: str
    total_delta: float
    bot_delta: float
    user_delta: float


@dataclass(frozen=True, slots=True)
class WorkerPlan:
    """Output of the predictive scheduler: how many workers to run."""

    workers: int                                # 1-5
    reason: str                                 # human-readable explanation
    confidence: float                           # 0.0-1.0 pattern model confidence
    predicted_user_usage: float = 0.0           # predicted user delta over horizon
    headroom_at_horizon: float = 0.0            # estimated headroom at end of plan


@dataclass(frozen=True, slots=True)
class PatternSlot:
    """One entry in the weekly usage heatmap."""

    day_of_week: int                            # 0=Monday, 6=Sunday
    hour: int                                   # 0-23
    slot: int                                   # 0-11 (5-min slot within the hour)
    avg_user_delta: float                       # average user utilization delta
    sample_count: int                           # observations for this slot


# ---------------------------------------------------------------------------
# Protocols — structural typing for dependency injection / testing
# ---------------------------------------------------------------------------


@runtime_checkable
class ClaudeGatewayProtocol(Protocol):
    """Interface for the Claude Agent SDK gateway."""

    async def invoke(
        self,
        prompt: str,
        *,
        phase: Phase,
        model: str,
        allowed_tools: list[str],
        cwd: str,
        timeout: float,
        priority: Priority = Priority.DIAGNOSTIC,
        max_turns: int | None = None,
    ) -> AgentResult: ...


@runtime_checkable
class GitHubCLIProtocol(Protocol):
    """Interface for gh/git CLI operations."""

    async def run_gh(self, args: list[str], cwd: str | None = None) -> CLIResult: ...
    async def run_git(self, args: list[str], cwd: str | None = None) -> CLIResult: ...
    async def run_cmd(self, cmd: list[str], cwd: str | None = None, timeout: float = 60.0) -> CLIResult: ...
    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]: ...


@runtime_checkable
class MemoryDBProtocol(Protocol):
    """Interface for the SQLite memory layer."""

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int: ...
    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None: ...
    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]: ...
    async def fetchval(self, sql: str, params: tuple[Any, ...] = ()) -> Any: ...

    async def get_repo_fact(self, repo: str, key: str) -> str | None: ...
    async def set_repo_fact(
        self, repo: str, key: str, value: str, source: str, confidence: float = 0.5
    ) -> None: ...
    async def get_outcome(self, repo: str, issue_number: int) -> dict[str, Any] | None: ...
    async def record_outcome(
        self,
        repo: str,
        issue_number: int,
        pr_number: int | None,
        outcome: Outcome,
        failure_reason: str | None = None,
        tokens_used: int = 0,
        iteration_count: int = 0,
    ) -> None: ...
    async def is_repo_banned(self, repo: str) -> bool: ...
    async def ban_repo(self, repo: str, reason: str, days: int, created_by: str) -> None: ...
    async def close(self) -> None: ...
    async def record_skill(
        self, repo: str, issue_number: int, issue_type: str | None,
        language: str | None, pattern: str | None, diff_summary: str,
        title: str | None = None,
    ) -> int: ...
    async def get_relevant_skills(
        self, issue_type: str | None, language: str | None, limit: int = 2,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class BalancerProtocol(Protocol):
    """Interface for the token management balancer."""

    @property
    def current_workers(self) -> int: ...

    @property
    def should_prefer_sonnet(self) -> bool: ...

    async def update(self) -> None: ...
