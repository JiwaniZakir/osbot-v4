"""Learning engine -- self-diagnostics, lesson extraction, benchmarking, and prompt variants."""

from osbot.learning.benchmark import benchmark_active_pool, benchmark_repo
from osbot.learning.diagnostics import deep_diagnostic, fast_diagnostic
from osbot.learning.lessons import on_feedback, on_merge, on_rejection
from osbot.learning.prompt_variants import seed_variants, select_variant

__all__ = [
    "fast_diagnostic",
    "deep_diagnostic",
    "benchmark_repo",
    "benchmark_active_pool",
    "on_merge",
    "on_rejection",
    "on_feedback",
    "seed_variants",
    "select_variant",
]
