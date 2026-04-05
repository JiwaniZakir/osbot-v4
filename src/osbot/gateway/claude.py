"""ClaudeGateway — Agent SDK wrapper with priority queue.

All Claude calls route through ``ClaudeGateway.invoke()``.  The gateway
maintains an internal ``asyncio.PriorityQueue`` and multiple background
consumer tasks (default 3).  ``invoke()`` enqueues the call at the given
*priority* and awaits an ``asyncio.Future`` that a consumer resolves
once the call completes.  This guarantees that higher-priority work
(feedback responses, critic calls) always executes before lower-priority
work (lesson extraction, diagnostics), while enabling concurrent
execution of up to ``max_concurrent`` calls.

If ``claude_agent_sdk`` is not installed, a stub gateway is provided
that always returns ``AgentResult(success=False, error="sdk_not_installed")``.

Implements ``ClaudeGatewayProtocol`` from ``osbot.types``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from osbot.config import settings
from osbot.log import get_logger
from osbot.types import AgentResult, Phase, Priority

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

try:
    import claude_agent_sdk  # type: ignore[import-untyped]

    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    logger.warning("claude_agent_sdk_missing", msg="Agent SDK not installed — ClaudeGateway will return stub errors")


# ---------------------------------------------------------------------------
# Internal queue item
# ---------------------------------------------------------------------------

_seq_counter: int = 0


def _next_seq() -> int:
    global _seq_counter
    _seq_counter += 1
    return _seq_counter


@dataclass(order=True)
class _CallItem:
    """Wrapper so PriorityQueue can compare items.

    Lower ``priority`` value = higher urgency.  ``seq`` breaks ties
    within the same priority level (FIFO).
    """

    priority: int
    seq: int = field(compare=True)
    future: asyncio.Future[AgentResult] = field(compare=False, repr=False)
    kwargs: dict[str, Any] = field(compare=False, repr=False, default_factory=dict)
    label: str = field(compare=False, default="")


class ClaudeGateway:
    """Agent SDK wrapper with priority queue and concurrent consumers.

    Satisfies ``ClaudeGatewayProtocol``.

    Parameters
    ----------
    claude_binary:
        Path to the ``claude`` CLI binary.  Defaults to ``settings.claude_binary``.
    on_call_complete:
        Optional callback ``(tokens_used, model) -> None`` invoked after every
        successful call so the balancer can update the decay model.
    max_concurrent:
        Maximum number of concurrent Claude calls.  Defaults to 3.
    """

    def __init__(
        self,
        claude_binary: str | None = None,
        on_call_complete: Callable[[int, str], None] | None = None,
        max_concurrent: int = 3,
    ) -> None:
        self._binary = claude_binary or settings.claude_binary
        self._on_call_complete = on_call_complete
        self._max_concurrent = max_concurrent

        # Priority queue and background consumers
        self._queue: asyncio.PriorityQueue[_CallItem] = asyncio.PriorityQueue()
        self._consumer_tasks: list[asyncio.Task[None]] = []
        self._shutting_down = False

    # -- Lifecycle -----------------------------------------------------------

    def _ensure_consumers(self) -> None:
        """Start background consumers if they are not already running."""
        # Remove completed/cancelled tasks
        self._consumer_tasks = [t for t in self._consumer_tasks if not t.done()]
        # Start consumers up to max_concurrent
        loop = asyncio.get_running_loop()
        while len(self._consumer_tasks) < self._max_concurrent:
            idx = len(self._consumer_tasks)
            task = loop.create_task(self._consumer(idx), name=f"claude-gateway-consumer-{idx}")
            self._consumer_tasks.append(task)

    async def shutdown(self) -> None:
        """Stop background consumers gracefully.

        Cancels all consumer tasks.  Pending items in the queue get their
        futures resolved with an error so callers are not stuck.
        """
        self._shutting_down = True
        for task in self._consumer_tasks:
            if not task.done():
                task.cancel()
        for task in self._consumer_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._consumer_tasks.clear()

        # Drain any remaining items and resolve their futures with an error
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if not item.future.done():
                    item.future.set_result(
                        AgentResult(
                            success=False,
                            text="",
                            tool_trace=[],
                            error="gateway_shutdown",
                            tokens_used=0,
                            model=item.kwargs.get("model", ""),
                        )
                    )
            except asyncio.QueueEmpty:
                break

    # -- Background consumers ------------------------------------------------

    async def _consumer(self, worker_id: int) -> None:
        """Process queued calls in priority order.

        Each consumer runs indefinitely, pulling the next highest-priority
        item when it becomes free.  Multiple consumers allow concurrent
        execution of up to ``max_concurrent`` calls.
        """
        logger.info("gateway_consumer_started", worker_id=worker_id)
        try:
            while True:
                item = await self._queue.get()
                logger.debug(
                    "gateway_dequeue",
                    worker_id=worker_id,
                    label=item.label,
                    priority=item.priority,
                    queue_size=self._queue.qsize(),
                )
                try:
                    result = await self._execute(**item.kwargs)
                    if not item.future.done():
                        item.future.set_result(result)
                except Exception as exc:
                    if not item.future.done():
                        item.future.set_result(
                            AgentResult(
                                success=False,
                                text="",
                                tool_trace=[],
                                error=str(exc),
                                tokens_used=0,
                                model=item.kwargs.get("model", ""),
                            )
                        )
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            logger.info("gateway_consumer_stopped", worker_id=worker_id)
            raise

    # -- ClaudeGatewayProtocol -----------------------------------------------

    async def invoke(
        self,
        prompt: str,
        *,
        phase: Phase,
        model: str,
        allowed_tools: list[str],
        cwd: str | None = None,
        timeout: float,
        priority: Priority = Priority.DIAGNOSTIC,
        max_turns: int | None = None,
    ) -> AgentResult:
        """Enqueue a Claude call and await its result.

        The call is placed on the internal priority queue.  A background
        consumer processes calls one at a time in priority order.  The
        caller's coroutine suspends on an ``asyncio.Future`` that is
        resolved when the call completes.
        """
        if not _SDK_AVAILABLE:
            return AgentResult(
                success=False,
                text="",
                tool_trace=[],
                error="sdk_not_installed",
                tokens_used=0,
                model=model,
            )

        # Ensure consumers are running
        self._ensure_consumers()

        label = f"{phase.value}/{priority.name}"
        logger.info(
            "claude_invoke_enqueue",
            phase=phase.value,
            model=model,
            timeout=timeout,
            priority=priority.name,
            queue_size=self._queue.qsize(),
        )

        # Create a future the caller will await
        loop = asyncio.get_running_loop()
        future: asyncio.Future[AgentResult] = loop.create_future()

        item = _CallItem(
            priority=int(priority),
            seq=_next_seq(),
            future=future,
            kwargs={
                "prompt": prompt,
                "phase": phase,
                "model": model,
                "allowed_tools": allowed_tools,
                "cwd": cwd,
                "timeout": timeout,
                "max_turns": max_turns,
            },
            label=label,
        )
        await self._queue.put(item)

        # Await the result -- the consumer will resolve this future
        return await future

    # -- Execution (called by consumer) --------------------------------------

    async def _execute(
        self,
        prompt: str,
        *,
        phase: Phase,
        model: str,
        allowed_tools: list[str],
        cwd: str | None = None,
        timeout: float,
        max_turns: int | None = None,
    ) -> AgentResult:
        """Execute a single Claude call via the Agent SDK.

        Streams events, captures tool-use traces, enforces timeout, and
        records token consumption.
        """
        logger.info(
            "claude_execute_start",
            phase=phase.value,
            model=model,
            timeout=timeout,
            cwd=cwd,
        )

        result_text = ""
        tool_trace: list[dict[str, Any]] = []
        tokens_used = 0

        try:
            async with asyncio.timeout(timeout):
                # Build ClaudeAgentOptions — the SDK's query() takes prompt + options
                from claude_agent_sdk.types import ClaudeAgentOptions

                opts_kwargs: dict[str, Any] = {
                    "model": model,
                    "allowed_tools": allowed_tools,
                    "cwd": cwd or "/tmp",
                    "cli_path": self._binary,
                    "permission_mode": "bypassPermissions",
                }
                if max_turns is not None:
                    opts_kwargs["max_turns"] = max_turns

                options = ClaudeAgentOptions(**opts_kwargs)
                stream = claude_agent_sdk.query(prompt=prompt, options=options)

                # Collect text from assistant messages (for no-tool calls like critic)
                assistant_texts: list[str] = []

                # Import SDK types for isinstance checks
                from claude_agent_sdk.types import (
                    AssistantMessage as SDKAssistantMessage,
                )
                from claude_agent_sdk.types import (
                    ResultMessage as SDKResultMessage,
                )

                async for event in stream:
                    if isinstance(event, SDKResultMessage):
                        result_text = event.result or ""
                        usage = event.usage or {}
                        if isinstance(usage, dict):
                            tokens_used = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

                    elif isinstance(event, SDKAssistantMessage):
                        for block in event.content or []:
                            if hasattr(block, "text"):
                                assistant_texts.append(block.text)
                            elif hasattr(block, "name"):
                                tool_trace.append(
                                    {
                                        "tool": block.name,
                                        "input": getattr(block, "input", {}),
                                    }
                                )

                # Use assistant text if result text is empty
                if not result_text and assistant_texts:
                    result_text = "\n".join(assistant_texts)

            logger.info(
                "claude_execute_done",
                phase=phase.value,
                model=model,
                tokens=tokens_used,
                tools_called=len(tool_trace),
            )

            # Notify the balancer about token consumption.
            if self._on_call_complete is not None:
                try:
                    self._on_call_complete(tokens_used, model)
                except Exception:
                    logger.warning("on_call_complete_failed", phase=phase.value)

            return AgentResult(
                success=True,
                text=result_text,
                tool_trace=tool_trace,
                error=None,
                tokens_used=tokens_used,
                model=model,
            )

        except TimeoutError:
            logger.warning(
                "claude_execute_timeout",
                phase=phase.value,
                model=model,
                timeout=timeout,
            )
            return AgentResult(
                success=False,
                text=result_text,
                tool_trace=tool_trace,
                error="timeout",
                tokens_used=tokens_used,
                model=model,
            )

        except Exception as exc:
            error_str = str(exc)
            logger.error(
                "claude_execute_error",
                phase=phase.value,
                model=model,
                error=error_str,
            )

            # Detect auth failures and notify owner instead of silently failing
            if "401" in error_str or "expired" in error_str.lower() or "authenticate" in error_str.lower():
                try:
                    from osbot.comms.blocker import notify_blocker

                    _loop = asyncio.get_running_loop()
                    _loop.create_task(notify_blocker("auth_expired"))
                except Exception:
                    pass  # Don't let notification failure crash the gateway

            return AgentResult(
                success=False,
                text=result_text,
                tool_trace=tool_trace,
                error=error_str,
                tokens_used=tokens_used,
                model=model,
            )

    # -- Introspection -------------------------------------------------------

    @property
    def queue_size(self) -> int:
        """Number of calls waiting in the queue."""
        return self._queue.qsize()
