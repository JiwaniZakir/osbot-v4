"""BotState — asyncio.Lock-protected in-memory state with JSON persistence.

Holds the issue queue, active work slots, and open PR list.  Every
mutation flushes to ``state.json`` so state survives restarts.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from typing import TYPE_CHECKING

from osbot.config import settings
from osbot.log import get_logger
from osbot.types import OpenPR, ScoredIssue

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = get_logger(__name__)


class BotState:
    """Async-safe in-memory state backed by state.json."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or settings.state_json_path
        self._lock = asyncio.Lock()
        self.issue_queue: list[ScoredIssue] = []
        self.active_work: dict[str, ScoredIssue] = {}  # key = "owner/repo#number"
        self.open_prs: list[OpenPR] = []

    # -- persistence ---------------------------------------------------------

    async def load(self) -> None:
        """Load state from disk.  Missing file is fine (fresh start).

        A corrupt ``state.json`` (truncated / non-JSON / wrong shape) must not
        crash-loop the container. The corrupt file is renamed aside for
        forensics so the next flush writes a fresh file rather than silently
        overwriting whatever's there, and the in-memory state stays empty.
        """
        if not self._path.exists():
            return
        async with self._lock:
            try:
                raw = self._path.read_text()
                data = json.loads(raw)
                self.issue_queue = [ScoredIssue(**i) for i in data.get("issue_queue", [])]
                self.active_work = {k: ScoredIssue(**v) for k, v in data.get("active_work", {}).items()}
                self.open_prs = [OpenPR(**p) for p in data.get("open_prs", [])]
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
                quarantine = self._path.with_suffix(f".corrupt-{int(time.time())}")
                try:
                    self._path.rename(quarantine)
                except OSError as rename_exc:
                    logger.error(
                        "state_quarantine_failed",
                        path=str(self._path),
                        error=str(rename_exc),
                    )
                logger.error(
                    "state_load_corrupt",
                    path=str(self._path),
                    quarantine=str(quarantine),
                    error=str(exc),
                )
                self.issue_queue = []
                self.active_work = {}
                self.open_prs = []

    async def _flush(self) -> None:
        """Write current state to disk.  Caller must hold ``_lock``."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "issue_queue": [asdict(i) for i in self.issue_queue],
            "active_work": {k: asdict(v) for k, v in self.active_work.items()},
            "open_prs": [asdict(p) for p in self.open_prs],
        }
        # Atomic write: write to tmp then rename
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.rename(self._path)

    # -- issue queue ---------------------------------------------------------

    @staticmethod
    def _issue_key(issue: ScoredIssue) -> str:
        return f"{issue.repo}#{issue.number}"

    async def pop_issue(
        self,
        predicate: Callable[[ScoredIssue], bool] | None = None,
    ) -> ScoredIssue | None:
        """Remove and return the highest-scored issue matching *predicate*.

        Returns ``None`` if the queue is empty or nothing matches.
        """
        async with self._lock:
            candidates = self.issue_queue if predicate is None else [i for i in self.issue_queue if predicate(i)]
            if not candidates:
                return None
            best = max(candidates, key=lambda i: i.score)
            self.issue_queue.remove(best)
            await self._flush()
            return best

    async def pop_and_mark_active(self) -> ScoredIssue | None:
        """Atomically pop the best issue whose repo is not already active.

        Prevents concurrent workers from racing to start two contributions
        to the same repo simultaneously, which causes duplicate PRs.

        Returns ``None`` if the queue is empty or all repos are already active.
        """
        async with self._lock:
            active_repos = {i.repo for i in self.active_work.values()}
            candidates = [i for i in self.issue_queue if i.repo not in active_repos]
            if not candidates:
                return None
            best = max(candidates, key=lambda i: i.score)
            self.issue_queue.remove(best)
            self.active_work[self._issue_key(best)] = best
            await self._flush()
            return best

    # Maximum issues per repo allowed in the queue at once.
    # Prevents discovery from flooding the queue with 5+ issues from the
    # same repo, which causes repeated failures before cooldown kicks in.
    _MAX_PER_REPO = 2

    async def enqueue(self, issues: list[ScoredIssue]) -> None:
        """Add issues to the queue, replacing duplicates with higher scores.

        Enforces a per-repo cap of ``_MAX_PER_REPO`` queued issues to
        prevent the same repo from dominating the queue.
        """
        async with self._lock:
            existing = {self._issue_key(i): i for i in self.issue_queue}
            for issue in issues:
                key = self._issue_key(issue)
                if key not in existing or issue.score > existing[key].score:
                    existing[key] = issue

            # Apply per-repo cap: keep only the top-scored issues per repo
            from collections import defaultdict

            by_repo: dict[str, list] = defaultdict(list)
            for issue in existing.values():
                by_repo[issue.repo].append(issue)

            capped: dict[str, ScoredIssue] = {}
            for repo_issues in by_repo.values():
                top = sorted(repo_issues, key=lambda i: i.score, reverse=True)[: self._MAX_PER_REPO]
                for i in top:
                    capped[self._issue_key(i)] = i

            self.issue_queue = list(capped.values())
            await self._flush()

    # -- active work ---------------------------------------------------------

    async def mark_active(self, issue: ScoredIssue) -> None:
        """Move an issue from the queue into active work slots."""
        key = self._issue_key(issue)
        async with self._lock:
            self.active_work[key] = issue
            await self._flush()

    async def complete(self, issue: ScoredIssue, outcome: str) -> None:
        """Remove an issue from active work.  *outcome* logged elsewhere."""
        key = self._issue_key(issue)
        async with self._lock:
            self.active_work.pop(key, None)
            await self._flush()

    async def clear_active(self) -> None:
        """Remove all active work (zombie cleanup on startup)."""
        async with self._lock:
            self.active_work.clear()
            await self._flush()

    # -- open PRs ------------------------------------------------------------

    async def add_open_pr(self, pr: OpenPR) -> None:
        """Track a newly submitted PR."""
        async with self._lock:
            # Deduplicate by pr_number
            self.open_prs = [p for p in self.open_prs if p.pr_number != pr.pr_number]
            self.open_prs.append(pr)
            await self._flush()

    async def remove_pr(self, pr_number: int) -> None:
        """Stop tracking a PR (merged, closed, or abandoned)."""
        async with self._lock:
            self.open_prs = [p for p in self.open_prs if p.pr_number != pr_number]
            await self._flush()

    async def get_open_prs(self) -> list[OpenPR]:
        """Return a snapshot of all tracked PRs."""
        async with self._lock:
            return list(self.open_prs)

    # -- queue management ----------------------------------------------------

    async def remove_queued_for_repo(self, repo: str) -> int:
        """Remove all queued (not yet started) issues for *repo*.

        Called after a PR is submitted to prevent the bot from immediately
        attempting more issues on the same repo, which triggers repeated
        cooldown bans before the first PR has had a chance to be reviewed.

        Returns the number of issues removed.
        """
        async with self._lock:
            before = len(self.issue_queue)
            self.issue_queue = [i for i in self.issue_queue if i.repo != repo]
            removed = before - len(self.issue_queue)
            if removed:
                await self._flush()
            return removed
