"""osbot v4 configuration — pydantic-settings with OSBOT_ prefix.

All values have sensible defaults. No hardcoded repo lists.
Override via environment variables: OSBOT_MAX_WORKERS=3, OSBOT_CYCLE_INTERVAL_SEC=300, etc.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration. All fields prefixed with OSBOT_ in env."""

    @model_validator(mode="after")
    def _validate_ceilings(self) -> Settings:
        """Ensure token ceilings are within valid bounds."""
        for name in ("five_hour_ceiling", "seven_day_ceiling", "opus_ceiling"):
            val = getattr(self, name)
            if not 0.0 < val <= 1.0:
                msg = f"{name}={val} must be in (0.0, 1.0]"
                raise ValueError(msg)
        if self.max_workers < 1:
            msg = f"max_workers={self.max_workers} must be >= 1"
            raise ValueError(msg)
        return self

    model_config = {"env_prefix": "OSBOT_", "frozen": True}

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------
    state_dir: Path = Path("state")
    workspaces_dir: Path = Path("workspaces")
    claude_binary: str = "claude"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "memory.db"

    @property
    def state_json_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def traces_path(self) -> Path:
        return self.state_dir / "traces.jsonl"

    @property
    def corrections_path(self) -> Path:
        return self.state_dir / "corrections.jsonl"

    # -----------------------------------------------------------------------
    # Orchestrator
    # -----------------------------------------------------------------------
    cycle_interval_sec: int = 600  # 10 min main loop
    discover_interval_sec: int = 1800  # 30 min
    review_interval_sec: int = 3600  # 1 hour
    engage_interval_sec: int = 1800  # 30 min
    learn_interval_sec: int = 43200  # 12 hours

    # -----------------------------------------------------------------------
    # Concurrency — max_workers used by balancer/scheduler for worker cap
    # -----------------------------------------------------------------------
    max_workers: int = 5

    # -----------------------------------------------------------------------
    # Token management
    # -----------------------------------------------------------------------
    five_hour_ceiling: float = 0.60  # Used by balancer + scheduler
    seven_day_ceiling: float = 0.50  # Used by balancer + scheduler
    opus_ceiling: float = 0.40  # Used by balancer
    plan_horizon_hours: float = 2.0  # Used by scheduler
    estimated_window_capacity: int = 2_000_000  # Used by balancer, scheduler, decay
    timezone: str = "US/Eastern"  # Used by scheduler + pattern

    # -----------------------------------------------------------------------
    # Cold start fallback (before pattern model has confidence)
    # -----------------------------------------------------------------------
    cold_start_workers_peak: int = 2  # 9am-6pm weekdays
    cold_start_workers_off: int = 4  # evenings/weekends

    # -----------------------------------------------------------------------
    # Discovery
    # -----------------------------------------------------------------------
    allowed_languages: list[str] = Field(
        default=["Python", "TypeScript"],
    )
    domain_keywords: list[str] = Field(
        default=[
            "ai",
            "llm",
            "ml",
            "rag",
            "agent",
            "transformer",
            "langchain",
            "embeddings",
            "vector",
            "nlp",
            "deep-learning",
            "machine-learning",
            "neural",
            "gpt",
            "openai",
            "anthropic",
        ],
    )
    repo_min_stars: int = 200
    repo_max_stars: int = 30_000
    repo_max_push_age_days: int = 30
    active_pool_max: int = 100
    repo_score_threshold: float = 4.0

    # -----------------------------------------------------------------------
    # Issue scoring
    # -----------------------------------------------------------------------
    maintainer_confirmed_bonus: float = 1.50
    issue_base_score: float = 5.0

    # -----------------------------------------------------------------------
    # Pipeline — Claude call timeouts
    # -----------------------------------------------------------------------
    implementation_timeout_sec: float = 600.0  # Call #1 (10 min — 180s caused 80% timeouts)
    critic_timeout_sec: float = 120.0  # Call #2
    pr_writer_timeout_sec: float = 60.0  # Call #3
    feedback_reader_timeout_sec: float = 60.0  # Call #4
    patch_applier_timeout_sec: float = 120.0  # Call #5

    # -----------------------------------------------------------------------
    # Quality gates
    # -----------------------------------------------------------------------
    max_diff_lines: int = 80
    max_files_changed: int = 3
    max_commit_message_len: int = 100  # relaxed from 72 — was rejecting valid commits 3 chars over
    min_commit_message_len: int = 10

    # -----------------------------------------------------------------------
    # Iteration — used by iteration/patcher.py
    # -----------------------------------------------------------------------
    max_iteration_rounds: int = 3
    max_iteration_growth: float = 1.2  # 120% of original size

    # -----------------------------------------------------------------------
    # Assignment
    # -----------------------------------------------------------------------
    assignment_timeout_hours: int = 72

    # -----------------------------------------------------------------------
    # Circuit breakers — thresholds are hardcoded in learning/diagnostics.py
    # for now.  These settings reserved for future configurability.
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # Models — each used by the corresponding pipeline/iteration module
    # -----------------------------------------------------------------------
    implementation_model: str = "sonnet"  # pipeline/implementer.py
    critic_model: str = "opus"  # pipeline/critic.py
    critic_fallback_model: str = "sonnet"  # pipeline/critic.py (when prefer_sonnet)
    pr_writer_model: str = "sonnet"  # pipeline/pr_writer.py
    feedback_reader_model: str = "sonnet"  # iteration/feedback.py
    patch_applier_model: str = "sonnet"  # iteration/patcher.py

    # -----------------------------------------------------------------------
    # GitHub — used by pipeline/preflight, submitter, assignment, intel/duplicates
    # -----------------------------------------------------------------------
    github_username: str = ""

    # -----------------------------------------------------------------------
    # Notifications
    # -----------------------------------------------------------------------
    webhook_url: str = ""  # Optional Discord/Slack webhook
    alert_email: str = "zj77@drexel.edu"  # Email for critical alerts
    email_webhook_url: str = ""  # Webhook-to-email bridge URL (Zapier/IFTTT/Make)


settings = Settings()
