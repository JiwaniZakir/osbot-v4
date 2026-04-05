"""Tests for banned AI phrase detection and scrubbing.

Covers detection of banned phrases, clean text passthrough, and scrubbing.
"""

from __future__ import annotations

from osbot.comms.phrases import BANNED_PHRASES, contains_banned, scrub_banned


async def test_contains_banned_detects_phrases() -> None:
    """Text containing banned phrases should be detected."""
    text = "I'd be happy to help fix this bug. It's worth noting that the issue is in parser.py."
    found = contains_banned(text)
    assert "I'd be happy to" in found
    assert "It's worth noting that" in found
    assert len(found) >= 2


async def test_clean_text_passes() -> None:
    """Text without any banned phrases should return an empty list."""
    text = "Fixed the missing import in parser.py by adding `import re` at the top of the file."
    found = contains_banned(text)
    assert found == []


async def test_scrub_removes_phrases() -> None:
    """Scrubbing should remove banned phrases while preserving real content."""
    text = "I'd be happy to help. The bug is in parser.py line 42."
    scrubbed = scrub_banned(text)
    assert "I'd be happy to" not in scrubbed
    assert "parser.py" in scrubbed
    assert "line 42" in scrubbed


async def test_scrub_collapses_whitespace() -> None:
    """Scrubbing should collapse leftover whitespace."""
    text = "Certainly, the fix involves changing the condition.  Absolutely, the test should pass."
    scrubbed = scrub_banned(text)
    assert "Certainly" not in scrubbed
    assert "Absolutely" not in scrubbed
    # Should not have runs of multiple spaces
    assert "  " not in scrubbed


async def test_contains_banned_case_insensitive() -> None:
    """Detection should be case-insensitive."""
    text = "I'D BE HAPPY TO help with this."
    found = contains_banned(text)
    assert len(found) >= 1


async def test_all_categories_have_entries() -> None:
    """The banned phrases list should contain entries from multiple categories."""
    # At minimum, we know there are 40+ phrases
    assert len(BANNED_PHRASES) >= 40


async def test_scrub_on_clean_text_is_noop() -> None:
    """Scrubbing clean text should return it unchanged (minus strip)."""
    text = "Fixed the null check in parse_input() to handle empty strings."
    scrubbed = scrub_banned(text)
    assert scrubbed == text
