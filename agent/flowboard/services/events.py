"""Per-board event bus for live updates streamed to the frontend."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class BoardBus:
    def __init__(self) -> None:
        self._queues: dict[int, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, board_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[board_id].append(q)
        return q

    def unsubscribe(self, board_id: int, q: asyncio.Queue) -> None:
        if q in self._queues.get(board_id, []):
            self._queues[board_id].remove(q)

    async def publish(self, board_id: int, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data}
        for q in list(self._queues.get(board_id, [])):
            await q.put(payload)


board_bus = BoardBus()
