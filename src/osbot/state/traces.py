"""TraceWriter — append-only JSONL logs for traces and corrections.

Traces record every contribution attempt.  Corrections record every
self-diagnostic action.  Both are append-only for auditability.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict
from typing import TYPE_CHECKING

from osbot.config import settings
from osbot.types import Correction, Trace

if TYPE_CHECKING:
    from pathlib import Path


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
        with self._traces_path.open("a") as f:
            f.write(line)

    async def read_recent_traces(self, n: int) -> list[Trace]:
        """Read the last *n* traces from traces.jsonl.

        Uses a bounded deque to avoid loading the entire file into memory
        for large histories.
        """
        if not self._traces_path.exists():
            return []
        recent: deque[str] = deque(maxlen=n)
        with self._traces_path.open() as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    recent.append(stripped)
        return [Trace(**json.loads(raw)) for raw in recent]

    # -- corrections ---------------------------------------------------------

    async def write_correction(self, correction: Correction) -> None:
        """Append a correction record to corrections.jsonl."""
        self._ensure_parent(self._corrections_path)
        line = json.dumps(asdict(correction), separators=(",", ":")) + "\n"
        with self._corrections_path.open("a") as f:
            f.write(line)

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
        return [Correction(**json.loads(raw)) for raw in recent]
