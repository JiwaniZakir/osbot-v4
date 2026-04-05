"""osbot.pipeline -- contribution engine (3 Claude calls per attempt).

Orchestrates: preflight -> [assignment] -> implement -> quality gates
-> critic -> PR description -> submit.
"""

from __future__ import annotations

from osbot.pipeline.run import run_pipeline

__all__ = ["run_pipeline"]
