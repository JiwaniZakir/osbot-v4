"""TraceWriter — append-only JSONL logs for traces and corrections.

Traces record every contribution attempt.  Corrections record every
self-diagnostic action.  Both are append-only for auditability.
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import asdict
from typing import TYPE_CHECKING

from osbot.config import settings
from osbot.log import get_logger
from osbot.types import Correction, Trace

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


def _append_line_atomic(path: Path, line: str) -> None:
    # Single ``os.write`` on an ``O_APPEND`` fd is POSIX-atomic up to
    # ``PIPE_BUF`` (>= 4096 on Linux / macOS). Trace + correction records
    # are a few hundred bytes, so a crash mid-write cannot leave a partial
    # record in the file. ``os.fsync`` flushes to disk to defend against a
    # host-level crash too.
    data = line.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


class TraceWriter:
    """Append-only JSONL writer for traces and corrections."""

    def __init__(
        self,
        traces_path: Path | None = None,
        corrections_path: Path | None = None,
    ) -> None:
        self._traces_path = traces_path or settings.traces_path
        self._corrections_path = corrections_path or settings.corrections_path

    def _ensure_parent(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    # -- traces --------------------------------------------------------------

    async def write_trace(self, trace: Trace) -> None:
        """Append a trace record to traces.jsonl."""
        self._ensure_parent(self._traces_path)
        line = json.dumps(asdict(trace), separators=(",", ":")) + "\n"
        _append_line_atomic(self._traces_path, line)

    async def read_recent_traces(self, n: int) -> list[Trace]:
        """Read the last *n* traces from traces.jsonl.

        Uses a bounded deque to avoid loading the entire file into memory
        for large histories. Malformed lines (from legacy non-atomic writes
        or unrelated corruption) are skipped with a warning rather than
        blowing up self-diagnostics.
        """
        if not self._traces_path.exists():
            return []
        recent: deque[str] = deque(maxlen=n)
        with self._traces_path.open() as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    recent.append(stripped)
        traces: list[Trace] = []
        skipped = 0
        for raw in recent:
            try:
                traces.append(Trace(**json.loads(raw)))
            except (json.JSONDecodeError, TypeError, ValueError):
                skipped += 1
        if skipped:
            logger.warning("traces_malformed_skipped", count=skipped, path=str(self._traces_path))
        return traces

    # -- corrections ---------------------------------------------------------

    async def write_correction(self, correction: Correction) -> None:
        """Append a correction record to corrections.jsonl."""
        self._ensure_parent(self._corrections_path)
        line = json.dumps(asdict(correction), separators=(",", ":")) + "\n"
        _append_line_atomic(self._corrections_path, line)

    async def read_recent_corrections(self, n: int) -> list[Correction]:
        """Read the last *n* corrections from corrections.jsonl."""
        if not self._corrections_path.exists():
            return []
        recent: deque[str] = deque(maxlen=n)
        with self._corrections_path.open() as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    recent.append(stripped)
        corrections: list[Correction] = []
        skipped = 0
        for raw in recent:
            try:
                corrections.append(Correction(**json.loads(raw)))
            except (json.JSONDecodeError, TypeError, ValueError):
                skipped += 1
        if skipped:
            logger.warning("corrections_malformed_skipped", count=skipped, path=str(self._corrections_path))
        return corrections
