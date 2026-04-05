"""Text utilities for prompt construction.

Layer 0 -- no internal dependencies.
"""

from __future__ import annotations


def truncate(text: str, max_chars: int, label: str) -> str:
    """Truncate *text* to *max_chars*, appending a note if shortened.

    Args:
        text: The text to truncate.
        max_chars: Maximum character count for the result.
        label: Human-readable label for the truncation note.

    Returns:
        The original text if within limits, or the truncated text with
        a trailing note: ``"...[{label} truncated from {original} to {max_chars} chars]"``.
    """
    if len(text) <= max_chars:
        return text
    original_len = len(text)
    note = f"...[{label} truncated from {original_len} to {max_chars} chars]"
    return text[:max_chars] + note
