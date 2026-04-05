"""State layer — all persistence for osbot v4.

Re-exports the three public components:

- ``MemoryDB`` — async SQLite with migrations
- ``BotState`` — asyncio.Lock-protected in-memory state (issue queue, active work, open PRs)
- ``TraceWriter`` — append-only JSONL logs (traces + corrections)
"""

from osbot.state.bot_state import BotState
from osbot.state.db import MemoryDB
from osbot.state.traces import TraceWriter

__all__ = ["BotState", "MemoryDB", "TraceWriter"]
