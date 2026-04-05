"""PR writer -- Call #3: generate the PR description.

``Closes #{number}`` is a template literal (injected by code, not Claude).
Body must include: file paths, function names, before/after, test output.
Specificity validation: at least 1 file path and 1 function name required.

Style variation: each PR gets a randomly selected structural style (seeded
by issue number for reproducibility) so descriptions don't all look identical.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass

from osbot.config import settings
from osbot.intel.policy import fetch_pr_template
from osbot.log import get_logger
from osbot.text import truncate
from osbot.types import (
    ClaudeGatewayProtocol,
    GitHubCLIProtocol,
    MemoryDBProtocol,
    Phase,
    Priority,
    ScoredIssue,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Style seeds -- structural variations for PR descriptions
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _StyleSeed:
    """A prompt variation that controls the structural ordering of a PR body."""
    name: str
    instruction: str


_STYLES: list[_StyleSeed] = [
    _StyleSeed(
        name="what_first",
        instruction=(
            "Structure the description as: WHAT changed (one-sentence summary, "
            "then a Changes section with files and functions), then WHY it was "
            "needed, then a Testing section.  Use ## headers for Changes, "
            "Motivation, and Testing."
        ),
    ),
    _StyleSeed(
        name="why_first",
        instruction=(
            "Structure the description as: WHY this change is needed (the root "
            "cause / motivation, 1-2 sentences), then WHAT changed (files, "
            "functions, line-level detail), then a Testing section.  Use ## "
            "headers for Motivation, Changes, and Testing."
        ),
    ),
    _StyleSeed(
        name="bug_narrative",
        instruction=(
            "Structure the description as a before/after narrative: describe the "
            "buggy behavior first (Before section), then describe the corrected "
            "behavior (After section), then list the specific code changes, then "
            "a Testing section.  Use ## headers for Before, After, Changes, and "
            "Testing."
        ),
    ),
    _StyleSeed(
        name="minimal",
        instruction=(
            "Write a minimal description: 2-3 concise sentences covering what "
            "changed and why, followed by a short list of modified files with "
            "the functions touched.  No section headers, no ## markers.  End "
            "with one sentence about how the fix was verified."
        ),
    ),
]


def _style_seed(issue_number: int) -> _StyleSeed:
    """Select a style variation deterministically based on issue number.

    Uses the issue number as a seed so the same issue always gets the same
    style (reproducible), but different issues get different styles.
    """
    rng = random.Random(issue_number)
    return rng.choice(_STYLES)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_prompt(
    issue: ScoredIssue,
    diff: str,
    pr_template: str | None = None,
    length_hint: str = "",
) -> str:
    """Build the PR description generation prompt with style variation."""
    style = _style_seed(issue.number)

    logger.debug("pr_writer_style", issue=issue.number, style=style.name)

    # If the repo has a PR template, instruct Claude to fill it in
    # instead of using our own structural style.
    if pr_template:
        template_section = (
            "IMPORTANT -- PR TEMPLATE COMPLIANCE:\n"
            "This repository requires PRs to follow a specific template.\n"
            "You MUST fill in all sections of the template below.\n"
            "For checkbox items (e.g., `- [ ]`), check the relevant ones with `- [x]`.\n"
            "Do NOT remove any sections from the template, even if they seem irrelevant -- "
            "leave them with a brief 'N/A' or appropriate default.\n"
            "Do NOT add extra sections beyond what the template specifies.\n\n"
            f"PR TEMPLATE:\n```\n{truncate(pr_template, 3000, 'PR template')}\n```\n\n"
            "Fill in the template above with details from the issue and diff.\n"
        )
        style_section = template_section
    else:
        style_section = f"STRUCTURAL STYLE:\n{style.instruction}\n"

    return f"""Write a pull request description for the following change.

ISSUE:
- Repository: {issue.repo}
- Issue #{issue.number}: {issue.title}
- Body: {truncate(issue.body, 3000, "issue body")}

DIFF:
```diff
{truncate(diff, 6000, "diff")}
```

{style_section}
REQUIREMENTS for the PR body:
1. Be specific -- reference actual file paths, function names, and line-level details from the diff.
2. Keep it concise -- no filler, no generic platitudes.
3. Include concrete testing information (test output, manual verification, etc.).
{length_hint}

DO NOT include:
- "Closes #N" or any issue reference (this will be added automatically)
- Phrases like "I'd be happy to", "feel free to", "let me know if"
- Generic descriptions that could apply to any PR
- Markdown headers larger than ## (no # headers)

Output ONLY the PR body text, no surrounding markdown fences."""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def write_pr(
    issue: ScoredIssue,
    diff: str,
    gateway: ClaudeGatewayProtocol,
    github: GitHubCLIProtocol | None = None,
    db: MemoryDBProtocol | None = None,
    downscoped: bool = False,
) -> str:
    """Call #3: generate the PR description body.

    Prepends ``Closes #{number}`` as a template literal (not generated
    by Claude).  Validates specificity before returning.

    If *github* is provided, fetches the repo's PR template (if any)
    and instructs Claude to fill it in, ensuring template compliance.

    If *db* is provided, reads benchmark data to calibrate PR description
    length to match the repo's conventions.

    If *downscoped* is True (Session 4 / ECHO), appends a note explaining
    that this PR addresses a sub-problem and remaining aspects may require
    separate work.

    Args:
        issue: The issue being fixed.
        diff: The unified diff.
        gateway: Claude gateway.
        github: Optional GitHub CLI for fetching PR templates.
        db: Optional memory DB for benchmark data.
        downscoped: Whether the implementation used downscope mode.

    Returns:
        The complete PR body string, ready for ``gh pr create --body``.
    """
    # Fetch PR template if GitHub CLI is available
    pr_template: str | None = None
    if github is not None:
        try:
            pr_template = await fetch_pr_template(issue.repo, github)
            if pr_template:
                logger.info(
                    "pr_writer_using_template",
                    repo=issue.repo,
                    template_len=len(pr_template),
                )
        except Exception as exc:
            logger.debug("pr_writer_template_fetch_error", repo=issue.repo, error=str(exc))

    # Calibrate description length from benchmark data
    length_hint = ""
    if db is not None:
        try:
            benchmark_raw = await db.get_repo_fact(issue.repo, "benchmark")
            if benchmark_raw:
                benchmark = json.loads(benchmark_raw)
                avg_body_len = benchmark.get("avg_pr_body_len", 0)
                if avg_body_len and avg_body_len < 200:
                    length_hint = "4. Keep the PR body under 200 words -- this repo prefers concise descriptions."
                elif avg_body_len and avg_body_len < 500:
                    length_hint = "4. Keep the PR body concise -- this repo averages short PR descriptions."
        except (json.JSONDecodeError, Exception):
            pass

    prompt = _build_prompt(issue, diff, pr_template=pr_template, length_hint=length_hint)

    logger.info("pr_writer_start", repo=issue.repo, issue=issue.number)

    result = await gateway.invoke(
        prompt,
        phase=Phase.CONTRIBUTE,
        model=settings.pr_writer_model,
        allowed_tools=[],  # PR writer gets no tools
        cwd="/tmp",
        timeout=settings.pr_writer_timeout_sec,
        priority=Priority.PR_WRITER,
        max_turns=1,
    )

    if not result.success:
        logger.warning(
            "pr_writer_failed",
            repo=issue.repo,
            issue=issue.number,
            error=result.error,
        )
        # Fallback: minimal but valid PR body
        return _fallback_body(issue, diff)

    body = result.text.strip()

    # Validate specificity
    if not _validate_specificity(body):
        logger.warning(
            "pr_writer_low_specificity",
            repo=issue.repo,
            issue=issue.number,
        )
        # Use it anyway -- low specificity is better than a fallback

    # Prepend Closes #N (template literal, not Claude-generated)
    # Append disclosure footer (GitHub AUP §3.2 compliance)
    disclosure = "\n\n---\n*This PR was created with AI assistance (Claude). The changes were reviewed by quality gates and a critic model before submission.*"
    full_body = f"Closes #{issue.number}\n\n{body}{disclosure}"

    # Session 4 (ECHO): When downscoped, append a note explaining
    # that this PR addresses a sub-problem of the issue.
    if downscoped:
        downscope_note = (
            f"\n\n---\n"
            f"**Note:** This PR addresses a specific sub-problem of "
            f"#{issue.number}. The remaining aspects may require separate work."
        )
        full_body += downscope_note
        logger.info(
            "pr_writer_downscope_note",
            repo=issue.repo,
            issue=issue.number,
        )

    logger.info(
        "pr_writer_done",
        repo=issue.repo,
        issue=issue.number,
        body_len=len(full_body),
        tokens=result.tokens_used,
        downscoped=downscoped,
    )

    return full_body


# ---------------------------------------------------------------------------
# Validation & fallback
# ---------------------------------------------------------------------------


def _validate_specificity(body: str) -> bool:
    """Check that the PR body has at least 1 file path and 1 function name.

    These are minimum specificity requirements to ensure the description
    references actual code, not generic platitudes.
    """
    has_file_path = bool(re.search(r"[\w/]+\.\w{1,4}", body))
    has_func_name = bool(
        re.search(r"(?:def |function |class |`\w+\(|`\w+`)", body)
    )
    return has_file_path and has_func_name


def _fallback_body(issue: ScoredIssue, diff: str) -> str:
    """Generate a minimal but valid PR body when Claude fails."""
    # Extract changed files from diff
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4:
                files.append(parts[3].lstrip("b/"))

    files_section = "\n".join(f"- `{f}`" for f in files[:5]) if files else "- (see diff)"

    return f"""Closes #{issue.number}

## Summary

Fix for: {issue.title}

## Changes

{files_section}

## Testing

Verified against existing test suite."""
