# osbot.gateway

## Purpose

Single point of contact for all Claude API calls. Wraps the Claude Agent SDK (`claude_agent_sdk`) with a priority queue, timeout management, and usage recording. Every Claude call in the entire bot routes through `ClaudeGateway.invoke()`. This is Layer 2 -- it depends on state (for usage recording) and nothing higher.

## Key Interfaces

```python
class AgentResult:
    """Returned by every gateway call."""
    success: bool
    text: str
    tool_trace: list[dict]    # [{"tool": name, "input": input}, ...]
    error: str | None         # "timeout", exception message, or None
    tokens_used: int


class ClaudeGateway:
    """Agent SDK wrapper with priority queue."""

    async def invoke(
        self,
        prompt: str,
        *,
        phase: str,                          # "implement", "critic", "pr_writer", etc.
        model: str = "sonnet",               # "sonnet" or "opus"
        allowed_tools: list[str] | None,     # Tool allowlist for Agent SDK
        cwd: str | None = None,              # Working directory for file tools
        timeout: int = 180,                  # Seconds
        priority: int = 5,                   # Lower = higher priority
    ) -> AgentResult:
        """Queue and execute a Claude call. Records usage to token system."""


class GatewayProtocol(Protocol):
    """Protocol for dependency injection in tests."""
    async def invoke(self, prompt: str, *, phase: str, model: str,
                     allowed_tools: list[str] | None, cwd: str | None,
                     timeout: int, priority: int) -> AgentResult: ...
```

### Priority Levels

```
1: feedback_response  (maintainer waiting -- highest value)
2: critic             (blocking a ready contribution)
3: implementer        (active contribution)
4: patch_applier      (iteration)
5: pr_writer          (contribution tail)
6: claim_comment      (engagement)
7: lesson             (learning)
8: diagnostic         (lowest)
```

## Dependencies

- `osbot.config` -- `CLAUDE_BINARY` path, default timeouts
- `osbot.types` -- `AgentResult`
- `osbot.log` -- structured logging
- `osbot.state` -- `MemoryDB` (for recording usage snapshots via token system hook)

External: `claude_agent_sdk` (Agent SDK package).

## Internal Structure

- **`gateway.py`** -- `ClaudeGateway` class. Manages an `asyncio.PriorityQueue`. The `invoke()` method enqueues a request and awaits its completion. A background consumer task dequeues and executes calls sequentially (only one Claude call at a time -- the SDK/CLI handles concurrency internally). Each call is wrapped in `asyncio.timeout()`. On completion, records token usage to the decay model via a callback. Streams Agent SDK events, collecting `result` text and `tool_use` traces.

- **`protocols.py`** -- `GatewayProtocol` for type-safe dependency injection. All consumers (pipeline, iteration, learning) depend on the protocol, never the concrete class. Tests provide a `MockGateway` that returns canned `AgentResult` objects.

## How to Test

```python
class MockGateway:
    """Returns canned responses. Tracks calls for assertions."""
    def __init__(self, responses: dict[str, AgentResult]):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []  # (phase, prompt_snippet)

    async def invoke(self, prompt, *, phase, **kw) -> AgentResult:
        self.calls.append((phase, prompt[:100]))
        return self.responses.get(phase, AgentResult(success=True, text="ok", tool_trace=[], error=None, tokens_used=0))

async def test_priority_ordering():
    gw = ClaudeGateway(...)
    # Enqueue low-priority, then high-priority
    # Assert high-priority executes first
```

- Never call the real Agent SDK in tests. Always use `MockGateway`.
- Test timeout behavior by making the mock `invoke` sleep longer than the timeout.
- Test priority ordering by enqueuing multiple requests and checking execution order.

## Design Decisions

1. **Single sequential consumer.** The Claude CLI already manages its own concurrency. Running multiple CLI calls simultaneously from the bot would race on the OAuth token and could cause TOS issues. One call at a time, prioritized.

2. **Priority queue, not FIFO.** Feedback responses must preempt new contributions. A maintainer waiting for a response is the highest-value moment.

3. **Protocol-based injection.** The gateway is the most-mocked dependency. Using a Protocol avoids importing the SDK in test environments where it may not be installed.

4. **Tool trace capture.** The critic needs to see the implementer's tool calls. Capturing the trace as structured data (not parsing text) makes this reliable.

5. **Usage recording at the gateway level.** Every call's token count is recorded here, not in each consumer. This guarantees the decay model never misses a call.
