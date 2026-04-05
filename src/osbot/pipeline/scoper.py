"""Two-pass scoping -- ask Claude to identify the minimal change before full implementation.

Pass 1 (this module): lightweight Claude call asking "what single file/function needs
changing?" Returns a ScopeHint string.

Pass 2 (implementer.py): The scope hint is injected as a hard constraint so Claude
knows exactly where to make changes.

Zero tools used in Pass 1 -- Claude reasons from the issue text alone.
This is cheap (~100 tokens total) and prevents the 94% scope failure rate.
"""

from __future__ import annotations

from osbot.config import settings
from osbot.log import get_logger
from osbot.types import ClaudeGatewayProtocol, Phase, Priority, ScoredIssue

logger = get_logger(__name__)


async def get_scope_hint(issue: ScoredIssue, gateway: ClaudeGatewayProtocol) -> str:
    """Ask Claude to identify the minimal change needed before full implementation.

    Uses a single no-tools Claude call (Pass 1) to determine the target file
    and function. The result is returned as a formatted constraint string
    suitable for injection into the implementer prompt (Pass 2).

    Args:
        issue: The scored issue to analyse.
        gateway: Claude gateway for the lightweight Agent SDK call.

    Returns:
        A formatted scope hint string if the issue appears SINGLE_FILE and the
        target is identifiable. Returns "" if the hint would not be useful
        (MULTI_FILE scope, both FILE and FUNCTION unknown, or gateway error).
    """
    prompt = f"""You are analyzing a GitHub issue to identify the minimal code change needed.

Issue: {issue.title}
Repository: {issue.repo}

Issue description (first 1500 chars):
{issue.body[:1500]}

Answer in EXACTLY this format (one line each):
FILE: <most likely file path or "UNKNOWN">
FUNCTION: <most likely function/method or "UNKNOWN">
CHANGE: <one sentence describing the minimal change>
SCOPE: <SINGLE_FILE or MULTI_FILE>

Rules:
- If you genuinely cannot determine from the issue description, use UNKNOWN
- If the fix clearly requires multiple files, say MULTI_FILE in SCOPE
- Be conservative -- a null check or off-by-one is almost always SINGLE_FILE
- Do NOT use tools. Reason from the issue text alone."""

    try:
        result = await gateway.invoke(
            prompt,
            phase=Phase.CONTRIBUTE,
            model=settings.implementation_model,
            allowed_tools=[],
            cwd=".",
            timeout=30.0,
            priority=Priority.IMPLEMENTER,
            max_turns=1,
        )
    except Exception as exc:
        logger.warning("scope_hint_gateway_error", repo=issue.repo, error=str(exc))
        return ""

    if not result.success or not result.text:
        logger.warning(
            "scope_hint_failed",
            repo=issue.repo,
            issue=issue.number,
            error=result.error,
        )
        return ""

    # Parse the structured response
    file_value = "UNKNOWN"
    function_value = "UNKNOWN"
    change_value = ""
    scope_value = "UNKNOWN"

    for line in result.text.splitlines():
        line = line.strip()
        if line.startswith("FILE:"):
            file_value = line[len("FILE:") :].strip()
        elif line.startswith("FUNCTION:"):
            function_value = line[len("FUNCTION:") :].strip()
        elif line.startswith("CHANGE:"):
            change_value = line[len("CHANGE:") :].strip()
        elif line.startswith("SCOPE:"):
            scope_value = line[len("SCOPE:") :].strip()

    logger.info(
        "scope_hint_computed",
        repo=issue.repo,
        scope=scope_value,
        file=file_value,
    )

    # Don't constrain genuinely multi-file issues
    if scope_value == "MULTI_FILE":
        return ""

    # Don't return a hint if both are unknown (no useful constraint)
    if file_value == "UNKNOWN" and function_value == "UNKNOWN":
        return ""

    return (
        "SCOPE ANALYSIS (pre-determined before implementation):\n"
        f"- Target file: {file_value}\n"
        f"- Target function: {function_value}\n"
        f"- Required change: {change_value}\n"
        "- Start your investigation in this file/function. "
        "Only expand to other files if absolutely necessary."
    )
