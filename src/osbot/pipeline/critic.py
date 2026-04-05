"""Critic -- Call #2: MAR-style review with actor tool trace.

HARD GATE: REJECT = permanently rejected, no retry.
Output: strict JSON ``{"verdict": "APPROVE"|"REJECT", "reason": "...", "issues": [...]}``.
"""

from __future__ import annotations

import json
from typing import Any

from osbot.config import settings
from osbot.log import get_logger
from osbot.text import truncate
from osbot.types import (
    ClaudeGatewayProtocol,
    CriticResult,
    CriticVerdict,
    Phase,
    Priority,
    ScoredIssue,
)

logger = get_logger(__name__)


def _format_tool_trace(tool_trace: list[dict[str, Any]], max_chars: int = 5000) -> str:
    """Format the implementer's tool trace for the critic prompt.

    Truncates oldest entries first to stay within *max_chars*, preserving
    the most recent tool calls which are most relevant to the final diff.
    """
    if not tool_trace:
        return "(no tool calls recorded)"

    lines: list[str] = []
    for i, call in enumerate(tool_trace, 1):
        tool = call.get("tool", "unknown")
        inp = call.get("input", {})
        # Truncate large inputs
        inp_str = json.dumps(inp, indent=None, separators=(",", ":"))
        if len(inp_str) > 300:
            inp_str = inp_str[:300] + "..."
        lines.append(f"  {i}. {tool}({inp_str})")

    result = "\n".join(lines)
    if len(result) <= max_chars:
        return result

    # Truncate oldest entries first (keep the tail)
    original_len = len(result)
    while len("\n".join(lines)) > max_chars and len(lines) > 1:
        lines.pop(0)
    note = f"  ...[tool trace truncated from {original_len} to ~{max_chars} chars, oldest entries removed]\n"
    return note + "\n".join(lines)


def _build_prompt(
    issue: ScoredIssue,
    diff: str,
    tool_trace: list[dict[str, Any]],
) -> str:
    """Build the MAR-style critic prompt."""
    trace_text = _format_tool_trace(tool_trace)

    return f"""You are a code review critic. An AI agent (the "actor") attempted to fix a GitHub issue.
Your job is to decide whether the fix should be submitted as a pull request.

ISSUE:
- Repository: {issue.repo}
- Issue #{issue.number}: {issue.title}
- Body: {truncate(issue.body, 3000, "issue body")}

ACTOR'S TOOL CALLS (what the actor did, in order):
{trace_text}

DIFF (what the actor changed):
```diff
{truncate(diff, 8000, "diff")}
```

REVIEW CRITERIA:
1. Does the diff fix the CORE SYMPTOM reported in the issue?
2. Does the diff contain UNRELATED CHANGES (different feature, different file for no reason)?
3. Are there CORRECTNESS BUGS, SECURITY RISKS, or DATA LOSS RISKS in the fix?
4. Does the diff avoid unnecessary reformatting of untouched code?
5. Is the change safe (no security issues, no data loss risk)?
6. [SOFT — non-blocking] If tests exist in the repo, did the actor add or update tests?

APPROVAL THRESHOLD:
- APPROVE if: #1=Yes AND #2=No AND #3=No AND #4=Yes (tests from #6 are a soft signal, not a blocker)
- REJECT ONLY if: the diff does not address the issue, OR contains unrelated changes, OR introduces bugs/risks
- Missing tests alone is NOT grounds for rejection — note it in issues[] but still APPROVE

You MUST respond with ONLY a JSON object in exactly this format (no other text, no markdown):

Example APPROVE response:
{{"verdict": "APPROVE", "reason": "Fix correctly addresses the null check issue", "issues": []}}

Example APPROVE with soft concern:
{{"verdict": "APPROVE", "reason": "Fix addresses the reported issue", "issues": ["no test added, but fix is correct"]}}

Example REJECT response:
{{"verdict": "REJECT", "reason": "Fix changes unrelated files", "issues": ["modified config.py which is unrelated to the reported issue"]}}

Rules:
- Your entire response must be a single JSON object. Nothing before or after it.
- verdict must be exactly "APPROVE" or "REJECT" (uppercase).
- reason is a one-sentence explanation.
- issues is an array of strings (empty array for APPROVE with no concerns).
- APPROVE means the PR is ready to submit as-is.
- REJECT means the fix has a fundamental problem (wrong target, unrelated changes, introduces bugs).
- Be critical but fair. Missing tests, minor style issues, and incomplete coverage are NOT rejection grounds.
- If the diff is empty or clearly does not address the issue, REJECT."""


async def review(
    issue: ScoredIssue,
    diff: str,
    tool_trace: list[dict[str, Any]],
    gateway: ClaudeGatewayProtocol,
    prefer_sonnet: bool = False,
) -> CriticResult:
    """Call #2: critic review.  HARD GATE.

    Args:
        issue: The issue being fixed.
        diff: The unified diff produced by the implementer.
        tool_trace: The implementer's tool call trace (for MAR-style review).
        gateway: Claude gateway.
        prefer_sonnet: If True, use sonnet instead of opus (budget conservation).

    Returns:
        CriticResult with verdict, reasoning, and specific issues.
    """
    prompt = _build_prompt(issue, diff, tool_trace)

    model = settings.critic_fallback_model if prefer_sonnet else settings.critic_model

    logger.info(
        "critic_start",
        repo=issue.repo,
        issue=issue.number,
        model=model,
        diff_lines=diff.count("\n"),
    )

    result = await gateway.invoke(
        prompt,
        phase=Phase.CONTRIBUTE,
        model=model,
        allowed_tools=[],  # Critic gets no tools -- pure text review
        cwd="/tmp",  # Needs a valid cwd even for no-tool calls
        timeout=settings.critic_timeout_sec,
        priority=Priority.CRITIC,
        max_turns=1,  # No tools = 1 turn is enough (just generate JSON)
    )

    if not result.success:
        logger.warning(
            "critic_failed",
            repo=issue.repo,
            issue=issue.number,
            error=result.error,
        )
        # On failure, default to REJECT (fail safe)
        return CriticResult(
            verdict=CriticVerdict.REJECT,
            reasoning=f"critic call failed: {result.error}",
            issues=["critic invocation error"],
        )

    # Parse the strict JSON output
    critic_result = _parse_critic_output(result.text)

    logger.info(
        "critic_done",
        repo=issue.repo,
        issue=issue.number,
        model=model,
        verdict=critic_result.verdict.value,
        reason=critic_result.reasoning[:100],
        tokens=result.tokens_used,
    )

    return critic_result


def _parse_critic_output(text: str) -> CriticResult:
    """Parse critic JSON output.  Tolerates markdown fences, extra text,
    and multiple JSON objects in output."""
    # Strip markdown code fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    # Strategy 1: Try direct parse (ideal case — pure JSON)
    try:
        data = json.loads(cleaned)
        return _build_critic_result(data)
    except (json.JSONDecodeError, TypeError):
        pass

    # Strategy 2: Extract first balanced JSON object using brace counting
    result = _extract_first_json_object(cleaned)
    if result is not None:
        return _build_critic_result(result)

    # Strategy 3: Classic first-{ to last-} extraction (handles single wrapped object)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
            return _build_critic_result(data)
        except json.JSONDecodeError:
            pass

    return CriticResult(
        verdict=CriticVerdict.REJECT,
        reasoning=f"critic output not valid JSON: {text[:200]}",
        issues=["malformed critic output"],
    )


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    """Find the first valid JSON object in text using balanced brace counting."""
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            in_string = False
            escape = False
            for j in range(i, len(text)):
                c = text[j]
                if escape:
                    escape = False
                    continue
                if c == "\\":
                    escape = True
                    continue
                if c == '"' and not escape:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(text[i : j + 1])
                            if isinstance(data, dict) and "verdict" in data:
                                return data
                        except json.JSONDecodeError:
                            pass
                        break
            i = j + 1 if depth == 0 else i + 1
        else:
            i += 1
    return None


def _build_critic_result(data: dict[str, Any]) -> CriticResult:
    """Build CriticResult from parsed JSON dict with strict field validation.

    Only reads the three expected keys (verdict, reason, issues).  Extra keys
    are ignored rather than propagated — this prevents unexpected Claude output
    from leaking into state or downstream prompts.
    """
    verdict_str = data.get("verdict", "REJECT")
    if not isinstance(verdict_str, str):
        verdict_str = "REJECT"
    verdict_str = verdict_str.upper().strip()
    try:
        verdict = CriticVerdict(verdict_str)
    except ValueError:
        verdict = CriticVerdict.REJECT

    reasoning = data.get("reason", data.get("reasoning", "no reason given"))
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)
    reasoning = reasoning[:500]  # cap length — no unbounded strings into state

    raw_issues = data.get("issues", [])
    if not isinstance(raw_issues, list):
        raw_issues = [str(raw_issues)]
    # Cap per-item length and total list size
    issues = [str(i)[:200] for i in raw_issues[:10] if i]

    return CriticResult(verdict=verdict, reasoning=reasoning, issues=issues)
