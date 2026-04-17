"""Quality gates -- post-implementation checks with zero Claude calls.

Validates: diff size, file count, test presence, no whole-file reformats,
linter, test suite, commit message format.  Each check is free.
"""

from __future__ import annotations

import re

from osbot.config import settings
from osbot.log import get_logger
from osbot.types import CLI_RC_NOT_FOUND, GitHubCLIProtocol, QualityGateResult

logger = get_logger(__name__)


async def run_gates(
    workspace: str,
    github: GitHubCLIProtocol,
) -> QualityGateResult:
    """Run all quality gates against the workspace.

    All checks are free (no Claude calls).  Returns a QualityGateResult
    with per-gate pass/fail details.
    """
    failures: list[str] = []

    # 1. Get diff stats
    diff_stat = await github.run_git(["diff", "--stat", "HEAD~1"], cwd=workspace)
    diff_full = await github.run_git(["diff", "HEAD~1"], cwd=workspace)

    diff_text = diff_full.stdout if diff_full.success else ""
    diff_lines = _count_diff_lines(diff_text)
    files_changed = _count_files_changed(diff_stat.stdout if diff_stat.success else "")

    # 2. Diff size check
    if diff_lines > settings.max_diff_lines:
        failures.append(f"diff too large: {diff_lines} lines (max {settings.max_diff_lines})")

    # 3. File count check
    if files_changed > settings.max_files_changed:
        failures.append(f"too many files changed: {files_changed} (max {settings.max_files_changed})")

    # 4. Whole-file reformat detection
    if _detect_reformat(diff_text):
        failures.append("detected whole-file reformat (whitespace-only changes dominate)")

    # 5. Test presence check (if repo has tests)
    tests_touched = _diff_touches_tests(diff_text)
    has_tests = await _repo_has_tests(workspace, github)
    if has_tests and not tests_touched:
        # Soft warning, not a hard failure -- some fixes don't need tests
        logger.info("quality_gate_warn", gate="no_test_touched", workspace=workspace)

    # 6. Commit message format
    commit_msg_result = await github.run_git(["log", "-1", "--format=%s"], cwd=workspace)
    commit_msg = commit_msg_result.stdout.strip() if commit_msg_result.success else ""
    msg_ok, msg_reason = _check_commit_message(commit_msg)
    if not msg_ok:
        failures.append(msg_reason)

    # 7. Lint check (best-effort, non-blocking for now)
    lint_passed = await _run_lint(workspace, github)

    # 8. Test suite (best-effort, non-blocking for now)
    tests_passed = await _run_tests(workspace, github)

    passed = len(failures) == 0
    result = QualityGateResult(
        passed=passed,
        failures=failures,
        diff_lines=diff_lines,
        files_changed=files_changed,
        tests_touched=tests_touched,
        lint_passed=lint_passed,
        tests_passed=tests_passed,
    )

    if passed:
        logger.info("quality_gates_pass", workspace=workspace, diff_lines=diff_lines, files=files_changed)
    else:
        logger.info("quality_gates_fail", workspace=workspace, failures=failures)

    return result


def _count_diff_lines(diff_text: str) -> int:
    """Count added + removed lines in a unified diff."""
    count = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++") or line.startswith("-") and not line.startswith("---"):
            count += 1
    return count


def _count_files_changed(stat_output: str) -> int:
    """Count files from ``git diff --stat`` output."""
    # Last line of git diff --stat is "N files changed, ..."
    lines = stat_output.strip().splitlines()
    if not lines:
        return 0
    # Each file line has " filename | N +++---"
    # The summary line contains "file(s) changed"
    count = 0
    for line in lines:
        if "|" in line:
            count += 1
    return count


def _detect_reformat(diff_text: str) -> bool:
    """Detect if the diff is primarily whitespace changes.

    Heuristic: if >80% of changed lines are whitespace-only, it's a reformat.
    """
    total = 0
    whitespace_only = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            total += 1
            if line[1:].strip() == "" or re.match(r"^\+\s*$", line):
                whitespace_only += 1
        elif line.startswith("-") and not line.startswith("---"):
            total += 1
            if line[1:].strip() == "" or re.match(r"^-\s*$", line):
                whitespace_only += 1

    if total < 10:
        return False
    return whitespace_only / total > 0.8


def _diff_touches_tests(diff_text: str) -> bool:
    """Check if any changed file is a test file."""
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            # Extract filename: "diff --git a/path b/path"
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3].lstrip("b/")
                if _is_test_file(path):
                    return True
    return False


def _is_test_file(path: str) -> bool:
    """Heuristic: file is a test if path contains test_ or _test or /tests/."""
    lower = path.lower()
    return (
        "test_" in lower
        or "_test." in lower
        or "/tests/" in lower
        or "/test/" in lower
        or lower.endswith("_test.py")
        or lower.endswith("_test.ts")
        or lower.endswith(".test.ts")
        or lower.endswith(".test.js")
    )


async def _repo_has_tests(workspace: str, github: GitHubCLIProtocol) -> bool:
    """Check if the repo has a tests directory."""
    result = await github.run_git(["ls-tree", "--name-only", "-d", "HEAD"], cwd=workspace)
    if not result.success:
        return False
    dirs = result.stdout.strip().splitlines()
    return any(d.lower() in ("tests", "test", "spec") for d in dirs)


def _check_commit_message(msg: str) -> tuple[bool, str]:
    """Validate commit message format."""
    if not msg:
        return False, "empty commit message"
    if len(msg) < settings.min_commit_message_len:
        return False, f"commit message too short: {len(msg)} chars (min {settings.min_commit_message_len})"
    if len(msg) > settings.max_commit_message_len:
        return False, f"commit message too long: {len(msg)} chars (max {settings.max_commit_message_len})"
    return True, ""


async def _run_lint(workspace: str, github: GitHubCLIProtocol) -> bool:
    # Only no-op when the binary is genuinely missing. Timeout / exception
    # sentinels (CLI_RC_TIMEOUT / CLI_RC_EXC) indicate the linter *ran* but
    # failed to produce a usable verdict, so we must fail closed rather than
    # let broken code through the gate.
    for cmd in [["ruff", "check", "."], ["flake8", "."]]:
        result = await github.run_cmd(cmd, cwd=workspace)
        if result.returncode == CLI_RC_NOT_FOUND:
            continue
        if not result.success:
            logger.info(
                "lint_failed",
                workspace=workspace,
                cmd=cmd,
                returncode=result.returncode,
                stderr=result.stderr[:200],
            )
        return result.success
    return True


async def _run_tests(workspace: str, github: GitHubCLIProtocol) -> bool:
    result = await github.run_cmd(["pytest", "--tb=short", "-q"], cwd=workspace, timeout=120.0)
    if result.returncode == CLI_RC_NOT_FOUND:
        return True
    if not result.success:
        logger.info(
            "tests_failed",
            workspace=workspace,
            returncode=result.returncode,
            stderr=result.stderr[:200],
        )
    return result.success
