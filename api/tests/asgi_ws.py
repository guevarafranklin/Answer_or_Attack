"""A minimal in-process WebSocket client for ASGI apps.

httpx has no WebSocket support and Starlette's TestClient runs the app in
another thread with its own event loop, which would cut the runtime task
off from the test's loop and DB connection. This speaks the ASGI
websocket protocol to the app directly, on the test's loop: the app runs
as a task, `receive`/`send` are two queues.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlencode


class Closed(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"websocket closed: {code} {reason}")
        self.code = code
        self.reason = reason


class WsClient:
    def __init__(self, app: Any, path: str, query: dict[str, str] | None = None) -> None:
        self._app = app
        self._scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": urlencode(query or {}).encode(),
            "headers": [(b"host", b"test")],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "subprotocols": [],
            "state": {},
        }
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.closed: Closed | None = None
        self.log: list[dict[str, Any]] = []  # every JSON message received, in order

    async def _receive(self) -> dict[str, Any]:
        return await self._to_app.get()

    async def _send(self, message: dict[str, Any]) -> None:
        await self._from_app.put(message)
        if message["type"] == "websocket.close":
            # A real server delivers the peer's close as a disconnect.
            await self._to_app.put({"type": "websocket.disconnect", "code": message.get("code", 1000)})

    async def open(self) -> WsClient:
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        await self._to_app.put({"type": "websocket.connect"})
        first = await self._from_app.get()
        if first["type"] == "websocket.close":
            self.closed = Closed(first.get("code", 1000), first.get("reason") or "")
            await self._task
            raise self.closed
        assert first["type"] == "websocket.accept", first
        return self

    async def send(self, message: dict[str, Any] | str) -> None:
        text = message if isinstance(message, str) else json.dumps(message)
        await self._to_app.put({"type": "websocket.receive", "text": text})

    async def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        """The next JSON message; raises Closed when the server closes."""
        if self.closed is not None:
            raise self.closed
        frame = await asyncio.wait_for(self._from_app.get(), timeout)
        if frame["type"] == "websocket.close":
            self.closed = Closed(frame.get("code", 1000), frame.get("reason") or "")
            raise self.closed
        message = json.loads(frame["text"])
        self.log.append(message)
        return message

    async def recv_type(self, kind: str, timeout: float = 5.0) -> dict[str, Any]:
        """Read until a message of type `kind` arrives (earlier ones stay
        in `log`)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"no {kind!r} message; log types: {[m['type'] for m in self.log]}")
            message = await self.recv(remaining)
            if message["type"] == kind:
                return message

    async def close(self) -> None:
        """The client hangs up."""
        if self.closed is None:
            await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            await self._task
