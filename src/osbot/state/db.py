"""MemoryDB — async SQLite persistence layer.

Wraps aiosqlite with dict-row access, a transaction context manager,
and automatic migration on connect.  Implements ``MemoryDBProtocol``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite

from osbot.state.migrations import run_migrations

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from osbot.types import Outcome


class MemoryDB:
    """Async SQLite database with dict rows and migration support."""

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._in_transaction: bool = False

    # -- lifecycle -----------------------------------------------------------

    async def connect(self, path: Path) -> None:
        """Open the database, enable WAL mode, and run pending migrations."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(path))
        self._db.row_factory = sqlite3.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await run_migrations(self)

    async def close(self) -> None:
        """Flush and close the connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    # -- helpers -------------------------------------------------------------

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("MemoryDB is not connected — call connect() first")
        return self._db

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row)

    # -- core API ------------------------------------------------------------

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """Execute a statement and return ``lastrowid``.

        Auto-commits unless inside a ``transaction()`` block.
        """
        conn = self._conn()
        cursor = await conn.execute(sql, params)
        if not self._in_transaction:
            await conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """Return the first row as a dict, or ``None``."""
        cursor = await self._conn().execute(sql, params)
        row = await cursor.fetchone()
        return self._row_to_dict(row)

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Return all rows as a list of dicts."""
        cursor = await self._conn().execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def fetchval(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        """Return the first column of the first row, or ``None``."""
        cursor = await self._conn().execute(sql, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return row[0]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Async context manager: BEGIN on entry, COMMIT on success, ROLLBACK on error.

        While inside this block, ``execute()`` will not auto-commit.
        """
        conn = self._conn()
        await conn.execute("BEGIN")
        self._in_transaction = True
        try:
            yield
            await conn.execute("COMMIT")
        except BaseException:
            await conn.execute("ROLLBACK")
            raise
        finally:
            self._in_transaction = False

    # -- business-logic methods (MemoryDBProtocol) ---------------------------

    @staticmethod
    def _utcnow() -> str:
        """ISO 8601 UTC timestamp string."""
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def get_repo_fact(self, repo: str, key: str) -> str | None:
        """Return the current (non-archived) value for *repo*/*key*, or ``None``."""
        return await self.fetchval(
            "SELECT value FROM repo_facts WHERE repo = ? AND key = ? AND valid_until IS NULL",
            (repo, key),
        )

    async def set_repo_fact(
        self,
        repo: str,
        key: str,
        value: str,
        source: str,
        confidence: float = 0.5,
    ) -> None:
        """Archive the old fact (if any) and insert a new one, atomically."""
        now = self._utcnow()
        async with self.transaction():
            # Archive the previous current fact
            await self.execute(
                "UPDATE repo_facts SET valid_until = ? WHERE repo = ? AND key = ? AND valid_until IS NULL",
                (now, repo, key),
            )
            # Insert the new fact
            await self.execute(
                """
                INSERT INTO repo_facts (repo, key, value, source, confidence, valid_from, valid_until, created_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (repo, key, value, source, confidence, now, now),
            )

    async def get_outcome(self, repo: str, issue_number: int) -> dict[str, Any] | None:
        """Return the most recent outcome row for *repo*/*issue_number*, or ``None``."""
        return await self.fetchone(
            "SELECT * FROM outcomes WHERE repo = ? AND issue_number = ? ORDER BY id DESC LIMIT 1",
            (repo, issue_number),
        )

    async def record_outcome(
        self,
        repo: str,
        issue_number: int,
        pr_number: int | None,
        outcome: Outcome,
        failure_reason: str | None = None,
        tokens_used: int = 0,
        iteration_count: int = 0,
    ) -> None:
        """Insert a new outcome row."""
        await self.execute(
            """
            INSERT INTO outcomes (repo, issue_number, pr_number, outcome, failure_reason,
                                  tokens_used, iteration_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (repo, issue_number, pr_number, outcome.value, failure_reason,
             tokens_used, iteration_count, self._utcnow()),
        )

    async def is_repo_banned(self, repo: str) -> bool:
        """Return ``True`` if *repo* has an active (non-expired) ban."""
        row = await self.fetchval(
            "SELECT 1 FROM repo_bans WHERE repo = ? AND expires_at > ? LIMIT 1",
            (repo, self._utcnow()),
        )
        return row is not None

    # -- progressive disclosure (Pattern 1 from claude-mem) ---------------------

    async def get_fact_index(self, repo: str) -> str:
        """Return a compact index of all current facts for a repo.

        This is ~50-100 tokens — suitable for injection into prompts.
        Claude can then request specific facts by key if needed.

        Format: "key1: summary | key2: summary | ..."
        """
        cached = await self.fetchval(
            "SELECT index_text FROM repo_fact_index WHERE repo = ?",
            (repo,),
        )
        if cached:
            return cached

        # Build from current facts
        return await self.rebuild_fact_index(repo)

    async def rebuild_fact_index(self, repo: str) -> str:
        """Rebuild the compact fact index for a repo from current facts.

        Called after any fact change. Stores the result for fast retrieval.
        """
        facts = await self.fetchall(
            "SELECT key, value FROM repo_facts WHERE repo = ? AND valid_until IS NULL ORDER BY key",
            (repo,),
        )
        if not facts:
            return ""

        # Build compact index: truncate values to keep total under ~100 tokens
        parts: list[str] = []
        for f in facts:
            key = f["key"]
            val = f["value"]
            # Truncate long values to 60 chars for the index
            short_val = val[:60] + "..." if len(val) > 60 else val
            parts.append(f"{key}: {short_val}")

        index_text = " | ".join(parts)

        # Cache it
        now = self._utcnow()
        await self.execute(
            """
            INSERT OR REPLACE INTO repo_fact_index (repo, index_text, fact_count, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (repo, index_text, len(facts), now),
        )
        return index_text

    async def set_repo_fact_with_index(
        self,
        repo: str,
        key: str,
        value: str,
        source: str,
        confidence: float = 0.5,
    ) -> None:
        """Set a repo fact AND rebuild the compact index.

        Prefer this over raw ``set_repo_fact`` when the index should stay current.
        """
        await self.set_repo_fact(repo, key, value, source, confidence)
        await self.rebuild_fact_index(repo)

    # -- outcome summaries (Pattern 2 from claude-mem) -------------------------

    async def record_outcome_with_summary(
        self,
        repo: str,
        issue_number: int,
        pr_number: int | None,
        outcome: Outcome,
        failure_reason: str | None = None,
        tokens_used: int = 0,
        iteration_count: int = 0,
        summary: str = "",
    ) -> None:
        """Record an outcome with an optional compressed narrative summary.

        The summary is a ~200-token AI-compressed description of what happened
        during the contribution attempt. Richer than failure_reason alone.
        """
        await self.execute(
            """
            INSERT INTO outcomes (repo, issue_number, pr_number, outcome, failure_reason,
                                  tokens_used, iteration_count, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (repo, issue_number, pr_number, outcome.value, failure_reason,
             tokens_used, iteration_count, summary, self._utcnow()),
        )

    async def get_recent_summaries(self, repo: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return recent outcome summaries for a repo (for lesson synthesis)."""
        return await self.fetchall(
            """
            SELECT outcome, failure_reason, summary, created_at
            FROM outcomes
            WHERE repo = ? AND summary != ''
            ORDER BY created_at DESC LIMIT ?
            """,
            (repo, limit),
        )

    # -- prompt variant meta-learning ------------------------------------------

    async def get_best_variant(
        self, section: str, repo_type: str = "general"
    ) -> dict[str, Any] | None:
        """Return the active variant with the highest success_rate for *section*.

        Falls back to ``general`` repo_type if no repo-specific variant exists.
        Returns ``None`` only if the table has no rows for *section* at all.
        """
        row = await self.fetchone(
            """
            SELECT * FROM prompt_variants
            WHERE prompt_section = ? AND repo_type = ? AND active = 1
            ORDER BY success_rate DESC, times_used ASC
            LIMIT 1
            """,
            (section, repo_type),
        )
        if row is not None:
            return row
        # Fallback to general when no repo-specific variant exists
        if repo_type != "general":
            return await self.fetchone(
                """
                SELECT * FROM prompt_variants
                WHERE prompt_section = ? AND repo_type = 'general' AND active = 1
                ORDER BY success_rate DESC, times_used ASC
                LIMIT 1
                """,
                (section,),
            )
        return None

    async def get_all_variants(
        self, section: str, repo_type: str = "general"
    ) -> list[dict[str, Any]]:
        """Return all active variants for *section*, falling back to general."""
        rows = await self.fetchall(
            """
            SELECT * FROM prompt_variants
            WHERE prompt_section = ? AND repo_type = ? AND active = 1
            """,
            (section, repo_type),
        )
        if rows:
            return rows
        if repo_type != "general":
            return await self.fetchall(
                """
                SELECT * FROM prompt_variants
                WHERE prompt_section = ? AND repo_type = 'general' AND active = 1
                """,
                (section,),
            )
        return rows

    async def record_variant_usage(self, variant_id: int, section: str) -> None:
        """Increment ``times_used`` for the variant identified by *variant_id*."""
        await self.execute(
            "UPDATE prompt_variants SET times_used = times_used + 1 WHERE id = ?",
            (variant_id,),
        )

    async def update_variant_stats(
        self, section: str, variant_name: str, success: bool
    ) -> None:
        """Update success statistics for a prompt variant.

        Increments ``times_success`` if *success*, then recomputes ``success_rate``
        from the stored totals.
        """
        if success:
            await self.execute(
                """
                UPDATE prompt_variants
                SET times_success = times_success + 1,
                    success_rate = CAST(times_success + 1 AS REAL) / MAX(times_used, 1)
                WHERE prompt_section = ? AND variant_name = ?
                """,
                (section, variant_name),
            )
        else:
            # Recompute success_rate even on failure (times_used was already
            # incremented by record_variant_usage at selection time).
            await self.execute(
                """
                UPDATE prompt_variants
                SET success_rate = CAST(times_success AS REAL) / MAX(times_used, 1)
                WHERE prompt_section = ? AND variant_name = ?
                """,
                (section, variant_name),
            )

    async def upsert_variant(
        self,
        section: str,
        variant_name: str,
        variant_text: str,
        repo_type: str = "general",
    ) -> None:
        """Insert a variant if it doesn't exist (by section + name + repo_type)."""
        existing = await self.fetchone(
            """
            SELECT id FROM prompt_variants
            WHERE prompt_section = ? AND variant_name = ? AND repo_type = ?
            """,
            (section, variant_name, repo_type),
        )
        if existing is None:
            await self.execute(
                """
                INSERT INTO prompt_variants
                    (prompt_section, variant_name, variant_text, repo_type, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (section, variant_name, variant_text, repo_type, self._utcnow()),
            )

    async def ban_repo(self, repo: str, reason: str, days: int, created_by: str) -> None:
        """Insert a time-bounded ban for *repo*."""
        now = datetime.now(UTC)
        expires = now + timedelta(days=days)
        await self.execute(
            """
            INSERT INTO repo_bans (repo, reason, banned_at, expires_at, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                repo,
                reason,
                now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                created_by,
            ),
        )

    # -- Reflexion (verbal self-feedback) ------------------------------------

    async def record_reflection(
        self,
        repo: str,
        issue_number: int,
        failure_phase: str,
        failure_reason: str,
        reflection: str,
        issue_type: str | None = None,
        issue_labels: list[str] | None = None,
        applicable_repos: list[str] | None = None,
    ) -> int:
        """Store a structured reflection after a pipeline rejection.

        Returns the rowid of the inserted reflection.
        """
        return await self.execute(
            """
            INSERT INTO reflections
                (repo, issue_number, failure_phase, failure_reason, reflection,
                 issue_type, issue_labels, applicable_repos, used_count,
                 led_to_success, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                repo,
                issue_number,
                failure_phase,
                failure_reason,
                reflection,
                issue_type,
                json.dumps(issue_labels) if issue_labels else None,
                json.dumps(applicable_repos) if applicable_repos else None,
                self._utcnow(),
            ),
        )

    async def get_relevant_reflections(
        self,
        repo: str,
        issue_type: str | None = None,
        labels: list[str] | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Retrieve reflections relevant to a new attempt.

        Matching priority (best match first):
        1. Same repo + same issue_type
        2. Same issue_type (cross-repo)
        3. Same repo (any issue_type)

        Returns at most *limit* reflections, ordered by relevance.
        """
        results: list[dict[str, Any]] = []

        # 1. Same repo + same issue_type (highest relevance)
        if issue_type:
            rows = await self.fetchall(
                """
                SELECT * FROM reflections
                WHERE repo = ? AND issue_type = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (repo, issue_type, limit),
            )
            results.extend(rows)

        # 2. Same issue_type, different repo (cross-repo transfer)
        if issue_type and len(results) < limit:
            remaining = limit - len(results)
            seen_ids = {r["id"] for r in results}
            rows = await self.fetchall(
                """
                SELECT * FROM reflections
                WHERE issue_type = ? AND repo != ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (issue_type, repo, remaining + len(seen_ids)),
            )
            for row in rows:
                if row["id"] not in seen_ids and len(results) < limit:
                    results.append(row)

        # 3. Same repo, any issue_type (fallback)
        if len(results) < limit:
            remaining = limit - len(results)
            seen_ids = {r["id"] for r in results}
            rows = await self.fetchall(
                """
                SELECT * FROM reflections
                WHERE repo = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (repo, remaining + len(seen_ids)),
            )
            for row in rows:
                if row["id"] not in seen_ids and len(results) < limit:
                    results.append(row)

        # Bump used_count for returned reflections
        for r in results:
            await self.execute(
                "UPDATE reflections SET used_count = used_count + 1 WHERE id = ?",
                (r["id"],),
            )

        return results

    # -- Step-Level Checkpoints (PRM) ----------------------------------------

    async def record_checkpoints(
        self,
        repo: str,
        issue_number: int,
        checkpoints: dict[str, bool],
        outcome_id: int | None = None,
    ) -> int:
        """Store a phase checkpoint row for a pipeline run.

        *checkpoints* maps phase names to pass/fail booleans:
        preflight_passed, implementation_completed, tests_pass,
        style_matches, diff_size_ok, scope_correct, critic_approves, pr_submitted.

        Returns the rowid of the inserted checkpoint.
        """
        return await self.execute(
            """
            INSERT INTO phase_checkpoints
                (repo, issue_number, outcome_id,
                 preflight_passed, implementation_completed, tests_pass,
                 style_matches, diff_size_ok, scope_correct,
                 critic_approves, pr_submitted, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repo,
                issue_number,
                outcome_id,
                int(checkpoints.get("preflight_passed", False)),
                int(checkpoints.get("implementation_completed", False)),
                int(checkpoints.get("tests_pass", False)),
                int(checkpoints.get("style_matches", False)),
                int(checkpoints.get("diff_size_ok", False)),
                int(checkpoints.get("scope_correct", False)),
                int(checkpoints.get("critic_approves", False)),
                int(checkpoints.get("pr_submitted", False)),
                self._utcnow(),
            ),
        )

    async def get_phase_stats(self, repo: str | None = None) -> dict[str, dict[str, int]]:
        """Compute per-phase pass/total counts from checkpoint data.

        Returns a dict like::

            {
                "preflight_passed": {"passed": 40, "total": 50},
                "implementation_completed": {"passed": 35, "total": 50},
                ...
            }

        If *repo* is given, restricts to that repo.
        """
        phases = [
            "preflight_passed",
            "implementation_completed",
            "tests_pass",
            "style_matches",
            "diff_size_ok",
            "scope_correct",
            "critic_approves",
            "pr_submitted",
        ]

        where_clause = "WHERE repo = ?" if repo else ""
        params: tuple[Any, ...] = (repo,) if repo else ()

        total_row = await self.fetchone(
            f"SELECT COUNT(*) as total FROM phase_checkpoints {where_clause}",
            params,
        )
        total = (total_row or {}).get("total", 0)

        stats: dict[str, dict[str, int]] = {}
        for phase in phases:
            row = await self.fetchone(
                f"SELECT SUM({phase}) as passed FROM phase_checkpoints {where_clause}",
                params,
            )
            passed = int((row or {}).get("passed", 0) or 0)
            stats[phase] = {"passed": passed, "total": total}

        return stats

    # -- Meta-lessons (A-Mem consolidation) -----------------------------------

    async def get_meta_lessons(
        self,
        lesson_type: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return meta-lessons, optionally filtered by type.

        Orders by confidence descending. Returns at most *limit* lessons.
        """
        if lesson_type:
            return await self.fetchall(
                """
                SELECT * FROM meta_lessons
                WHERE lesson_type = ?
                ORDER BY confidence DESC
                LIMIT ?
                """,
                (lesson_type, limit),
            )
        return await self.fetchall(
            """
            SELECT * FROM meta_lessons
            ORDER BY confidence DESC
            LIMIT ?
            """,
            (limit,),
        )

    async def upsert_meta_lesson(
        self,
        lesson_type: str,
        lesson_text: str,
        source_repos: list[str],
        confidence: float,
        source_outcome_ids: list[int] | None = None,
    ) -> None:
        """Insert or update a meta-lesson.

        If a lesson of the same *lesson_type* already exists, update its
        text, source repos, confidence, and timestamp. Otherwise insert
        a new row.
        """
        now = self._utcnow()
        repos_json = json.dumps(source_repos)
        outcome_ids_json = json.dumps(source_outcome_ids or [])

        existing = await self.fetchone(
            "SELECT id FROM meta_lessons WHERE lesson_type = ?",
            (lesson_type,),
        )
        if existing:
            await self.execute(
                """
                UPDATE meta_lessons
                SET lesson_text = ?, source_repos = ?, source_outcome_ids = ?,
                    confidence = ?, updated_at = ?
                WHERE id = ?
                """,
                (lesson_text, repos_json, outcome_ids_json, confidence, now, existing["id"]),
            )
        else:
            await self.execute(
                """
                INSERT INTO meta_lessons
                    (lesson_type, lesson_text, source_repos, source_outcome_ids,
                     confidence, success_rate, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0.0, ?, ?)
                """,
                (lesson_type, lesson_text, repos_json, outcome_ids_json,
                 confidence, now, now),
            )

    # -- Skill library (Voyager-style) ----------------------------------------

    async def record_skill(
        self,
        repo: str,
        issue_number: int,
        issue_type: str | None,
        language: str | None,
        pattern: str | None,
        diff_summary: str,
        title: str | None = None,
    ) -> int:
        """Store a successful diff snippet as a reusable skill.

        Returns the rowid of the inserted row.
        """
        return await self.execute(
            """
            INSERT INTO skills
                (repo, issue_number, issue_type, language, pattern,
                 diff_summary, title, used_count, led_to_success, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (repo, issue_number, issue_type, language, pattern,
             diff_summary, title, self._utcnow()),
        )

    async def get_relevant_skills(
        self,
        issue_type: str | None,
        language: str | None,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Return skills most relevant to the given issue_type and language.

        Ranks results so that rows matching both issue_type and language come
        first, then rows matching only issue_type, then everything else.
        Secondary sort: ``led_to_success`` DESC, then most recent first.
        """
        return await self.fetchall(
            """
            SELECT *,
              CASE WHEN issue_type = ? AND language = ? THEN 2
                   WHEN issue_type = ? THEN 1
                   ELSE 0 END as relevance
            FROM skills
            ORDER BY relevance DESC, led_to_success DESC, created_at DESC
            LIMIT ?
            """,
            (issue_type, language, issue_type, limit),
        )

    async def mark_skill_success(self, repo: str, issue_number: int) -> None:
        """Mark a skill as having led to a successful (merged) outcome."""
        await self.execute(
            "UPDATE skills SET led_to_success = 1 WHERE repo = ? AND issue_number = ?",
            (repo, issue_number),
        )
