"""Tests for critic JSON parsing -- _parse_critic_output.

Covers valid JSON, markdown fences, extra text, empty input, invalid JSON,
and missing fields.
"""

from __future__ import annotations

from osbot.pipeline.critic import _parse_critic_output
from osbot.types import CriticVerdict


async def test_parse_approve_json() -> None:
    """Standard APPROVE JSON should parse correctly."""
    text = '{"verdict": "APPROVE", "reason": "Fix is correct", "issues": []}'
    result = _parse_critic_output(text)
    assert result.verdict == CriticVerdict.APPROVE
    assert result.reasoning == "Fix is correct"
    assert result.issues == []


async def test_parse_reject_json() -> None:
    """Standard REJECT JSON with issues should parse correctly."""
    text = '{"verdict": "REJECT", "reason": "Scope creep", "issues": ["modified unrelated file", "no tests"]}'
    result = _parse_critic_output(text)
    assert result.verdict == CriticVerdict.REJECT
    assert result.reasoning == "Scope creep"
    assert len(result.issues) == 2
    assert "modified unrelated file" in result.issues


async def test_parse_with_markdown_fences() -> None:
    """JSON wrapped in ```json fences should be extracted correctly."""
    text = '```json\n{"verdict": "APPROVE", "reason": "Clean fix", "issues": []}\n```'
    result = _parse_critic_output(text)
    assert result.verdict == CriticVerdict.APPROVE
    assert result.reasoning == "Clean fix"


async def test_parse_with_extra_text() -> None:
    """JSON with surrounding text should be extracted via brace matching."""
    text = 'Here is my review:\n{"verdict": "REJECT", "reason": "Missing test", "issues": ["no test"]}\nThat is my assessment.'
    result = _parse_critic_output(text)
    assert result.verdict == CriticVerdict.REJECT
    assert result.reasoning == "Missing test"


async def test_parse_empty_returns_reject() -> None:
    """Empty input should default to REJECT (fail-safe)."""
    result = _parse_critic_output("")
    assert result.verdict == CriticVerdict.REJECT
    assert "malformed" in result.issues[0].lower() or "not valid" in result.reasoning.lower()


async def test_parse_invalid_json_returns_reject() -> None:
    """Garbled JSON should default to REJECT."""
    result = _parse_critic_output("{this is not valid json}")
    assert result.verdict == CriticVerdict.REJECT


async def test_parse_missing_verdict_defaults_reject() -> None:
    """JSON without a 'verdict' key should default to REJECT."""
    text = '{"reason": "looks good", "issues": []}'
    result = _parse_critic_output(text)
    # missing verdict -> defaults to "REJECT" via .get("verdict", "REJECT")
    assert result.verdict == CriticVerdict.REJECT


async def test_parse_lowercase_verdict_normalized() -> None:
    """Lowercase verdict should be normalized to uppercase."""
    text = '{"verdict": "approve", "reason": "fine", "issues": []}'
    result = _parse_critic_output(text)
    assert result.verdict == CriticVerdict.APPROVE


async def test_parse_reasoning_key_fallback() -> None:
    """'reasoning' key should be accepted as fallback for 'reason'."""
    text = '{"verdict": "APPROVE", "reasoning": "All good", "issues": []}'
    result = _parse_critic_output(text)
    assert result.reasoning == "All good"
