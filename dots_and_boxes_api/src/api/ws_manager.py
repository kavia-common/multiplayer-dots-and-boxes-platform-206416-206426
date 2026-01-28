from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class RoomConnectionManager:
    """Tracks active WebSocket connections per room and broadcasts messages."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, room_code: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[room_code].add(websocket)

    async def disconnect(self, room_code: str, websocket: WebSocket) -> None:
        async with self._lock:
            if room_code in self._connections and websocket in self._connections[room_code]:
                self._connections[room_code].remove(websocket)
            if room_code in self._connections and not self._connections[room_code]:
                self._connections.pop(room_code, None)

    async def broadcast(self, room_code: str, payload: Any) -> None:
        async with self._lock:
            conns = list(self._connections.get(room_code, set()))
        # Send outside lock; drop broken sockets.
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(room_code, ws)

    async def send(self, websocket: WebSocket, payload: Any) -> None:
        await websocket.send_json(payload)
