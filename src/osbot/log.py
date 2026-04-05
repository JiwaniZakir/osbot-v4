"""Structured logging for osbot v4 — structlog with JSON rendering.

Uses structlog for machine-readable JSON logs in production
and human-readable colored output in development (OSBOT_ENV=development).

Usage:
    from osbot.log import get_logger
    logger = get_logger("mymodule")
    logger.info("something happened", repo="owner/name", score=7.5)
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def _configure_once() -> None:
    """Set up structlog processors and stdlib integration. Idempotent."""
    if structlog.is_configured():
        return

    is_dev = os.environ.get("OSBOT_ENV", "production") == "development"

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        structlog.processors.format_exc_info,
    ]

    if is_dev:
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if is_dev else logging.INFO)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a named structured logger, configuring on first call.

    Args:
        name: Logger name, typically the module (e.g. "orchestrator", "pipeline.critic").

    Returns:
        A bound logger with the name pre-attached.
    """
    _configure_once()
    return structlog.get_logger(name)
