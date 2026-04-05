"""Banned AI phrase list and detection utility.

Contains 40+ phrases that are strong signals of AI-generated text.
Organised by category for maintainability.  The ``contains_banned``
function does case-insensitive substring matching and returns all
matched phrases.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Banned phrases by category
# ---------------------------------------------------------------------------

_GREETINGS: list[str] = [
    "I'd be happy to",
    "I'd be glad to",
    "I'm happy to",
    "Great question",
    "Good question",
    "Thanks for bringing this up",
    "Thanks for reporting this",
    "Thanks for the detailed report",
]

_FILLER: list[str] = [
    "Certainly",
    "Absolutely",
    "Of course",
    "Sure thing",
    "Definitely",
    "Indeed",
    "It's worth noting that",
    "It's important to note",
    "It should be noted that",
    "It's worth mentioning",
    "Interestingly",
]

_STRUCTURE: list[str] = [
    "Let me",
    "Allow me to",
    "I'll go ahead and",
    "I've gone ahead and",
    "Here's what I",
    "Here's my approach",
    "Here is what I",
    "Here is my approach",
    "I went ahead and",
]

_META: list[str] = [
    "As an AI",
    "As a language model",
    "I don't have personal",
    "I don't have access to",
    "Based on the information provided",
    "Based on my analysis",
]

_HEDGING: list[str] = [
    "I believe",
    "It seems like",
    "It appears that",
    "If I understand correctly",
    "If I'm not mistaken",
    "It looks like",
]

_CLOSING: list[str] = [
    "Feel free to",
    "Don't hesitate to",
    "Please don't hesitate",
    "Let me know if you need",
    "Let me know if there's anything",
    "Hope this helps",
    "I hope this helps",
    "Happy to help",
    "Happy coding",
]

# Promise phrases -- the bot must NEVER promise future actions it cannot fulfill.
# (e.g., "I'll add screenshots" then never following through)
_PROMISES: list[str] = [
    "I'll add",
    "I will add",
    "will update",
    "will fix",
    "will post",
    "shortly",
    "I'll get to",
    "I'll work on",
    "I'll update",
    "I will update",
    "I'll post",
    "I will post",
    "I'll fix",
    "I will fix",
    "I'll provide",
    "I will provide",
    "I'll include",
    "I will include",
    "I'll follow up",
    "I will follow up",
    "will follow up",
    "will get back",
    "I'll get back",
    "I will get back",
    "will share",
    "I'll share",
    "I will share",
    "working on it",
    "will do",
    "will address",
]

_EXCESSIVE_PRAISE: list[str] = [
    "Great catch",
    "Excellent question",
    "That's a great point",
    "Good catch",
    "Nice find",
    "Well spotted",
]

# Flat list for public consumption.
BANNED_PHRASES: list[str] = (
    _GREETINGS + _FILLER + _STRUCTURE + _META + _HEDGING + _CLOSING
    + _EXCESSIVE_PRAISE + _PROMISES
)

# Pre-compiled patterns (case-insensitive, word-boundary aware where practical).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (phrase, re.compile(re.escape(phrase), re.IGNORECASE))
    for phrase in BANNED_PHRASES
]


def contains_banned(text: str) -> list[str]:
    """Return all banned phrases found in *text* (case-insensitive).

    Returns an empty list when the text is clean.
    """
    return [phrase for phrase, pattern in _PATTERNS if pattern.search(text)]


def scrub_banned(text: str) -> str:
    """Remove every banned phrase from *text*, collapsing leftover whitespace.

    Phrases are removed (not replaced with an alternative) because the
    surrounding sentence usually reads fine without the filler.
    """
    result = text
    for _phrase, pattern in _PATTERNS:
        result = pattern.sub("", result)
    # Collapse runs of whitespace / orphaned punctuation.
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = re.sub(r"^ +", "", result, flags=re.MULTILINE)
    return result.strip()
