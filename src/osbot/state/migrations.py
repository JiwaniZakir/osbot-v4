"""Schema management for memory.db.

Migrations are numbered, sequential, and idempotent.  Each migration
is a list of SQL statements.  ``run_migrations`` applies any that
haven't been applied yet, inside a transaction per migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from osbot.state.db import MemoryDB

# ---------------------------------------------------------------------------
# Migration registry — append-only, never edit earlier entries
# ---------------------------------------------------------------------------

MIGRATIONS: list[list[str]] = [
    # --- Migration 1: initial schema (8 tables from v4 plan) ---
    [
        # Temporal, conflict-aware repo knowledge
        """
        CREATE TABLE IF NOT EXISTS repo_facts (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            superseded_by INTEGER,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_repo_facts_repo_key ON repo_facts(repo, key)",
        "CREATE INDEX IF NOT EXISTS idx_repo_facts_current ON repo_facts(repo, key, valid_until)",
        # PR attempt outcomes
        """
        CREATE TABLE IF NOT EXISTS outcomes (
            id INTEGER PRIMARY KEY,
            repo TEXT,
            issue_number INTEGER,
            pr_number INTEGER,
            outcome TEXT,
            failure_reason TEXT,
            tokens_used INTEGER,
            iteration_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_outcomes_repo ON outcomes(repo)",
        # Maintainer behavior profiles
        """
        CREATE TABLE IF NOT EXISTS maintainer_profiles (
            id INTEGER PRIMARY KEY,
            repo TEXT,
            username TEXT,
            avg_days_to_merge REAL,
            prefers_small_prs INTEGER,
            requests_tests INTEGER,
            last_active TEXT,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_profiles_repo ON maintainer_profiles(repo, username)",
        # External repo quality signals (7-day TTL)
        """
        CREATE TABLE IF NOT EXISTS repo_signals (
            repo TEXT PRIMARY KEY,
            external_merge_rate REAL,
            avg_response_hours REAL,
            close_completion_rate REAL,
            ci_enabled INTEGER,
            requires_assignment INTEGER,
            has_ai_policy INTEGER,
            last_computed TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """,
        # Circuit-breaker bans
        """
        CREATE TABLE IF NOT EXISTS repo_bans (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            reason TEXT NOT NULL,
            banned_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_by TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_bans_repo ON repo_bans(repo)",
        "CREATE INDEX IF NOT EXISTS idx_bans_expires ON repo_bans(expires_at)",
        # Token management: probe snapshots (7-day rolling)
        """
        CREATE TABLE IF NOT EXISTS usage_snapshots (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            five_hour REAL,
            seven_day REAL,
            opus_weekly REAL,
            sonnet_weekly REAL
        )
        """,
        # Token management: bot vs user decomposition
        """
        CREATE TABLE IF NOT EXISTS usage_deltas (
            id INTEGER PRIMARY KEY,
            period_start TEXT,
            period_end TEXT,
            total_delta REAL,
            bot_delta REAL,
            user_delta REAL
        )
        """,
        # Token management: weekly usage heatmap (learned)
        """
        CREATE TABLE IF NOT EXISTS user_pattern (
            day_of_week INTEGER,
            hour INTEGER,
            slot INTEGER,
            avg_user_delta REAL,
            sample_count INTEGER,
            PRIMARY KEY (day_of_week, hour, slot)
        )
        """,
    ],
    # --- Migration 2: outcome summaries + fact index for progressive disclosure ---
    [
        # Add compressed narrative summary column to outcomes
        # This stores a ~200-token AI-compressed summary of the full trace
        # alongside the structured fields. Richer data for lesson synthesis.
        "ALTER TABLE outcomes ADD COLUMN summary TEXT DEFAULT ''",
        # Fact index for progressive disclosure:
        # A compact, token-efficient index of all current facts for a repo.
        # Updated whenever facts change. The implementation prompt injects
        # ONLY this index (~50-100 tokens) instead of all facts (~500+).
        # Claude can then request specific facts by key via a tool call.
        """
        CREATE TABLE IF NOT EXISTS repo_fact_index (
            repo TEXT PRIMARY KEY,
            index_text TEXT NOT NULL,
            fact_count INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """,
    ],
    # --- Migration 3: Reflexion + Step-Level Checkpoints ---
    [
        # Verbal self-feedback reflections (Reflexion, NeurIPS).
        # On each rejection, we store a structured reflection about what
        # went wrong and why. Next time a similar issue is attempted,
        # matching reflections are injected into the implementation prompt.
        """
        CREATE TABLE IF NOT EXISTS reflections (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            failure_phase TEXT NOT NULL,
            failure_reason TEXT NOT NULL,
            reflection TEXT NOT NULL,
            issue_type TEXT,
            issue_labels TEXT,
            applicable_repos TEXT,
            used_count INTEGER DEFAULT 0,
            led_to_success INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_reflections_repo ON reflections(repo)",
        "CREATE INDEX IF NOT EXISTS idx_reflections_type ON reflections(issue_type)",
        "CREATE INDEX IF NOT EXISTS idx_reflections_phase ON reflections(failure_phase)",
        # Step-level checkpoints (RLVR/PRM).
        # Decomposes binary merge/reject into per-phase pass/fail signals
        # so we can identify which phase is the bottleneck.
        """
        CREATE TABLE IF NOT EXISTS phase_checkpoints (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            outcome_id INTEGER,
            preflight_passed INTEGER DEFAULT 0,
            implementation_completed INTEGER DEFAULT 0,
            tests_pass INTEGER DEFAULT 0,
            style_matches INTEGER DEFAULT 0,
            diff_size_ok INTEGER DEFAULT 0,
            scope_correct INTEGER DEFAULT 0,
            critic_approves INTEGER DEFAULT 0,
            pr_submitted INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_checkpoints_repo ON phase_checkpoints(repo)",
        "CREATE INDEX IF NOT EXISTS idx_checkpoints_outcome ON phase_checkpoints(outcome_id)",
    ],
    # --- Migration 4: Prompt variant meta-learning ---
    [
        # Track which prompt variants produce better outcomes.
        # Epsilon-greedy selection: 80% best variant, 20% exploration.
        # Only FORBIDDEN and TASK sections are varied; critic prompt stays stable.
        """
        CREATE TABLE IF NOT EXISTS prompt_variants (
            id INTEGER PRIMARY KEY,
            prompt_section TEXT NOT NULL,
            variant_name TEXT NOT NULL,
            variant_text TEXT NOT NULL,
            repo_type TEXT NOT NULL DEFAULT 'general',
            times_used INTEGER DEFAULT 0,
            times_success INTEGER DEFAULT 0,
            success_rate REAL DEFAULT 0.0,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_variants_section ON prompt_variants(prompt_section, repo_type)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_variants_unique ON prompt_variants(prompt_section, variant_name, repo_type)",
    ],
    # --- Migration 5: meta_lessons table (A-Mem consolidation) ---
    [
        # Cross-repo meta-lessons synthesized from outcome patterns.
        # When the same failure pattern appears in 3+ repos, we
        # consolidate it into a generalizable lesson (e.g., "scope creep
        # causes rejection") that gets injected into all implementation
        # prompts. Zero Claude calls -- pure SQL aggregation.
        """
        CREATE TABLE IF NOT EXISTS meta_lessons (
            id INTEGER PRIMARY KEY,
            lesson_type TEXT NOT NULL,
            lesson_text TEXT NOT NULL,
            source_repos TEXT NOT NULL DEFAULT '[]',
            source_outcome_ids TEXT NOT NULL DEFAULT '[]',
            confidence REAL DEFAULT 0.5,
            success_rate REAL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_meta_lessons_type ON meta_lessons(lesson_type)",
        "CREATE INDEX IF NOT EXISTS idx_meta_lessons_confidence ON meta_lessons(confidence DESC)",
    ],
    # --- Migration 6: Skill library (Voyager-style) ---
    [
        """
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            issue_type TEXT,
            language TEXT,
            pattern TEXT,
            diff_summary TEXT NOT NULL,
            title TEXT,
            used_count INTEGER DEFAULT 0,
            led_to_success INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_skills_type_lang ON skills(issue_type, language)",
        "CREATE INDEX IF NOT EXISTS idx_skills_pattern ON skills(pattern)",
    ],
]


async def run_migrations(db: MemoryDB) -> int:
    """Apply all pending migrations.  Returns count of newly applied migrations."""

    # Ensure the version-tracking table exists
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )

    current = await db.fetchval("SELECT COALESCE(MAX(version), 0) FROM schema_version")
    applied = 0

    for i, statements in enumerate(MIGRATIONS, start=1):
        if i <= current:
            continue
        async with db.transaction():
            for sql in statements:
                await db.execute(sql)
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, datetime('now'))",
                (i,),
            )
        applied += 1

    return applied
