"""Tests for quality gates -- post-implementation validation.

Tests the pure functions that do not require subprocess calls.
"""

from __future__ import annotations

from osbot.pipeline.quality import (
    _check_commit_message,
    _count_diff_lines,
    _count_files_changed,
    _detect_reformat,
    _diff_touches_tests,
    _is_test_file,
)


# ---------------------------------------------------------------------------
# Diff size
# ---------------------------------------------------------------------------


async def test_diff_too_large_rejected() -> None:
    """Diff with more added/removed lines than max should be counted correctly."""
    # Build a diff with 60 added lines (exceeds default max_diff_lines=50)
    lines = ["diff --git a/foo.py b/foo.py", "--- a/foo.py", "+++ b/foo.py"]
    for i in range(60):
        lines.append(f"+line {i}")
    diff_text = "\n".join(lines)
    count = _count_diff_lines(diff_text)
    assert count == 60
    assert count > 50  # Exceeds settings.max_diff_lines


async def test_diff_within_limit_passes() -> None:
    """Diff with 10 changed lines should be under the limit."""
    lines = ["diff --git a/foo.py b/foo.py", "--- a/foo.py", "+++ b/foo.py"]
    for i in range(5):
        lines.append(f"+added {i}")
    for i in range(5):
        lines.append(f"-removed {i}")
    diff_text = "\n".join(lines)
    count = _count_diff_lines(diff_text)
    assert count == 10
    assert count <= 50


# ---------------------------------------------------------------------------
# File count
# ---------------------------------------------------------------------------


async def test_too_many_files_rejected() -> None:
    """git diff --stat with 5 files should exceed the max of 3."""
    stat_output = (
        " src/a.py | 5 +++++\n"
        " src/b.py | 3 +++\n"
        " src/c.py | 2 ++\n"
        " src/d.py | 1 +\n"
        " src/e.py | 1 +\n"
        " 5 files changed, 12 insertions(+)\n"
    )
    count = _count_files_changed(stat_output)
    assert count == 5
    assert count > 3


async def test_file_count_single_file() -> None:
    """Single file changed should pass."""
    stat_output = " src/parser.py | 3 +++\n 1 file changed, 3 insertions(+)\n"
    count = _count_files_changed(stat_output)
    assert count == 1


# ---------------------------------------------------------------------------
# Commit message
# ---------------------------------------------------------------------------


async def test_commit_message_too_long() -> None:
    """Commit message longer than max should fail."""
    msg = "x" * 120  # Exceeds max_commit_message_len=100
    ok, reason = _check_commit_message(msg)
    assert not ok
    assert "too long" in reason


async def test_commit_message_ok() -> None:
    """Commit message within bounds should pass."""
    msg = "Fix missing import in parser module"
    ok, reason = _check_commit_message(msg)
    assert ok
    assert reason == ""


async def test_commit_message_too_short() -> None:
    """Commit message shorter than min should fail."""
    msg = "fix"
    ok, reason = _check_commit_message(msg)
    assert not ok
    assert "too short" in reason


async def test_commit_message_empty() -> None:
    """Empty commit message should fail."""
    ok, reason = _check_commit_message("")
    assert not ok
    assert "empty" in reason


# ---------------------------------------------------------------------------
# Reformat detection
# ---------------------------------------------------------------------------


async def test_detect_reformat_whitespace_only() -> None:
    """Diff with >80% whitespace-only changes should be detected as reformat."""
    lines = []
    for _ in range(20):
        lines.append("+   ")  # whitespace-only additions
    for _ in range(2):
        lines.append("+real code here")  # 2 real changes out of 22 = ~9%
    diff_text = "\n".join(lines)
    assert _detect_reformat(diff_text) is True


async def test_detect_reformat_real_changes() -> None:
    """Diff with real code changes should not be flagged."""
    lines = []
    for i in range(20):
        lines.append(f"+import module_{i}")
    diff_text = "\n".join(lines)
    assert _detect_reformat(diff_text) is False


# ---------------------------------------------------------------------------
# Test file detection
# ---------------------------------------------------------------------------


async def test_diff_touches_tests() -> None:
    """Diff that modifies a test file should be detected."""
    diff_text = "diff --git a/tests/test_parser.py b/tests/test_parser.py\n+new test line"
    assert _diff_touches_tests(diff_text) is True


async def test_diff_no_tests() -> None:
    """Diff that only touches source files should not flag tests."""
    diff_text = "diff --git a/src/parser.py b/src/parser.py\n+import re"
    assert _diff_touches_tests(diff_text) is False


async def test_is_test_file_patterns() -> None:
    """Various test file patterns should be recognized."""
    assert _is_test_file("tests/test_parser.py") is True
    assert _is_test_file("src/parser_test.py") is True
    assert _is_test_file("src/tests/unit/foo.py") is True  # /tests/ in path
    assert _is_test_file("src/parser.py") is False
    assert _is_test_file("components/Button.test.ts") is True
