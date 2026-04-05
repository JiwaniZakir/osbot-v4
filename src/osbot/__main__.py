"""osbot entry point — python -m osbot"""

from __future__ import annotations

import asyncio
import sys

from osbot.log import get_logger

logger = get_logger("osbot")


async def main() -> None:
    from osbot.config import Settings
    from osbot.orchestrator import run

    settings = Settings()
    logger.info("osbot_starting", version="4.0.0", cycle_interval=settings.cycle_interval_sec)

    await run()


def main_sync() -> None:
    """Synchronous wrapper for the console_scripts entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("osbot interrupted")
        sys.exit(0)


if __name__ == "__main__":
    main_sync()
