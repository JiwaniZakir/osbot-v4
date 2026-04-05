"""Prompt variant meta-learning (ICLR 2025 Prompt Optimization).

Track which prompt variants produce better outcomes across the FORBIDDEN
and TASK sections of the implementation prompt.  Uses epsilon-greedy
selection: 80% of the time pick the best-performing variant, 20% explore.

Constraint: max 2 variants per section initially.
Constraint: do NOT apply variants to the critic prompt (must stay stable).
"""

from __future__ import annotations

import random
from typing import Any

from osbot.config import settings
from osbot.log import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Exploration parameter
# ---------------------------------------------------------------------------

_EPSILON = 0.20  # 20% exploration, 80% exploitation


# ---------------------------------------------------------------------------
# Default variants — seeded on startup
# ---------------------------------------------------------------------------

# FORBIDDEN section variants
_FORBIDDEN_STRICT = (
    "- Do NOT touch files unrelated to the issue.\n"
    "- Do NOT add unnecessary imports.\n"
    "- Do NOT reformat entire files or change whitespace outside your fix.\n"
    "- Do NOT introduce new abstractions, classes, or modules unless the fix requires it.\n"
    "- Do NOT add docstrings to unchanged code.\n"
    '- Do NOT refactor surrounding code "while you\'re at it."\n'
    "- Do NOT modify CI configuration, build files, or package manifests unless the issue specifically requires it.\n"
    "- Do NOT create new test files if an existing test file covers the same module -- add to the existing file.\n"
    "- Do NOT add downstream workarounds or monkey-patches when the root cause can be fixed directly.\n"
    f"- Keep your total diff under {settings.max_diff_lines} lines and touch at most {settings.max_files_changed} files."
)

_FORBIDDEN_PERMISSIVE = (
    "- Do NOT touch files unrelated to the issue.\n"
    "- Do NOT add unnecessary imports.\n"
    "- Do NOT reformat entire files or change whitespace outside your fix.\n"
    '- Do NOT refactor surrounding code "while you\'re at it."\n'
    "- Do NOT modify CI configuration, build files, or package manifests unless the issue specifically requires it.\n"
    "- Do NOT add downstream workarounds or monkey-patches when the root cause can be fixed directly.\n"
    f"- Keep your total diff under {settings.max_diff_lines} lines and touch at most {settings.max_files_changed} files."
)

# TASK section variants
_TASK_MINIMAL_FIX = (
    "1. Read the relevant source files to understand the problem.\n"
    "2. Before writing code, identify WHERE in the code the bug originates "
    "(the root cause), not where the symptom appears. Trace the data flow "
    "backward from the symptom to find the earliest point where the logic goes wrong.\n"
    "3. Fix the ROOT CAUSE of the bug, not the symptom. If the issue is caused "
    "by incorrect logic upstream, fix it there -- don't add a workaround downstream.\n"
    "4. Implement the MINIMAL fix that resolves the issue.\n"
    "5. If the repo has tests, add or update a test that covers your fix.\n"
    "6. Run the existing test suite to confirm nothing is broken.\n"
    "7. Stage and commit your changes with a concise message (50-72 chars).\n"
    '8. Your commit message should be a single line in imperative mood (e.g., "Fix missing null check in parser").'
)

_TASK_ROOT_CAUSE_DEEP = (
    "1. Read the relevant source files to understand the problem.\n"
    "2. Trace the data flow from the point the issue describes. Walk backward "
    "through callers, constructors, and data transformations to find the EARLIEST "
    "point where the logic goes wrong. This is the root cause.\n"
    "3. Before writing any code, write a one-sentence hypothesis: "
    '"The root cause is in [file]:[function] because [reason]."\n'
    "4. Fix ONLY the root cause. Do not patch symptoms downstream.\n"
    "5. Implement the MINIMAL fix that resolves the issue.\n"
    "6. If the repo has tests, add or update a test that covers your fix.\n"
    "7. Run the existing test suite to confirm nothing is broken.\n"
    "8. Stage and commit your changes with a concise message (50-72 chars).\n"
    '9. Your commit message should be a single line in imperative mood (e.g., "Fix missing null check in parser").'
)


# Registry: section -> [(variant_name, variant_text)]
DEFAULT_VARIANTS: dict[str, list[tuple[str, str]]] = {
    "forbidden": [
        ("strict", _FORBIDDEN_STRICT),
        ("permissive", _FORBIDDEN_PERMISSIVE),
    ],
    "task": [
        ("minimal_fix", _TASK_MINIMAL_FIX),
        ("root_cause_deep", _TASK_ROOT_CAUSE_DEEP),
    ],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def seed_variants(db: Any) -> None:
    """Insert default variants if the prompt_variants table is empty.

    Safe to call on every startup -- skips insertion if variants already exist.
    Called from the orchestrator's startup sequence.
    """
    for section, variants in DEFAULT_VARIANTS.items():
        for variant_name, variant_text in variants:
            await db.upsert_variant(section, variant_name, variant_text)
    logger.info("prompt_variants_seeded", sections=list(DEFAULT_VARIANTS.keys()))


async def select_variant(
    section: str,
    repo_type: str,
    db: Any,
) -> tuple[str, str, int]:
    """Epsilon-greedy variant selection.

    80% of the time: pick the variant with the highest success_rate.
    20% of the time: pick a random variant (exploration).

    Args:
        section: The prompt section ("forbidden" or "task").
        repo_type: Repo classification ("general" by default).
        db: MemoryDB instance.

    Returns:
        Tuple of (variant_name, variant_text, variant_id).
        Falls back to the first default variant if DB has no data.
    """
    all_variants = await db.get_all_variants(section, repo_type)

    if not all_variants:
        # Table empty or not yet migrated -- return first default
        defaults = DEFAULT_VARIANTS.get(section, [])
        if defaults:
            name, text = defaults[0]
            logger.debug("variant_fallback_default", section=section, variant=name)
            return (name, text, -1)
        return ("unknown", "", -1)

    # Epsilon-greedy selection
    if random.random() < _EPSILON and len(all_variants) > 1:
        # Explore: pick uniformly at random
        chosen = random.choice(all_variants)
        logger.debug(
            "variant_explore",
            section=section,
            variant=chosen["variant_name"],
        )
    else:
        # Exploit: pick the variant with the highest success_rate
        # (already sorted by success_rate DESC from DB, but be explicit)
        chosen = max(all_variants, key=lambda v: (v["success_rate"], -v["times_used"]))
        logger.debug(
            "variant_exploit",
            section=section,
            variant=chosen["variant_name"],
            success_rate=chosen["success_rate"],
        )

    # Record usage
    variant_id = chosen["id"]
    await db.record_variant_usage(variant_id, section)

    return (chosen["variant_name"], chosen["variant_text"], variant_id)
