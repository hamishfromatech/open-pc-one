"""A minimal WSS client used by the end-to-end tests."""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any

from openpc_engine.auth import AUTH_PREFIX, sign_with
from websockets.asyncio.client import connect


class WssClient:
    def __init__(self, port: int, device_id: str, key: Any) -> None:
        self.port = port
        self.device_id = device_id
        self.key = key
        self.ws = None
        self._pending: list[dict[str, Any]] = []

    async def connect(self, authenticate: bool = True) -> dict[str, Any]:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.ws = await connect(f"wss://127.0.0.1:{self.port}", ssl=ctx, max_size=None)
        challenge = await self.recv(expect="host_challenge")
        if not authenticate:
            return challenge
        signature = sign_with(self.key, f"{AUTH_PREFIX}{challenge['nonce']}".encode())
        await self.send(
            {
                "type": "auth",
                "deviceId": self.device_id,
                "nonce": challenge["nonce"],
                "signature": signature,
            }
        )
        return await self.recv(expect="auth_ok")

    async def send(self, frame: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(frame))

    async def recv(self, timeout: float = 10.0, expect: str | None = None) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self._pending:
                frame = self._pending.pop(0)
            else:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"waiting for {expect or 'frame'}")
                raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
                frame = json.loads(raw)
            if expect is None or frame.get("type") == expect:
                return frame

    async def recv_until(self, predicate, timeout: float = 10.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            frame = await self.recv(timeout=max(0.01, deadline - asyncio.get_running_loop().time()))
            if predicate(frame):
                return frame

    async def subscribe(self, session_id: str = "main", timeout: float = 30.0) -> dict[str, Any]:
        await self.send({"type": "subscribe", "sessionId": session_id})
        return await self.recv(timeout=timeout, expect="host_hello")

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
