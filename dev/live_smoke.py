"""Live smoke test against a running openpc-engine on wss://127.0.0.1:8095.

Operator tool, not part of `make test`: it talks to a *running* edge over the
real wire (mint a code with `openpc-engine pair-code`, then pass it in).

Exercises the real wire protocol end to end: TLS handshake, host_challenge,
one-time pairing code, Ed25519 device auth, subscribe -> host_hello, a full
prompt turn (response + streamed deltas + agent_end), ping/pong, session_status,
list_sessions, and that an unauthenticated socket cannot read the session.

Usage: python live_smoke.py <pair-code>
"""

from __future__ import annotations

import asyncio
import base64
import json
import ssl
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.client import connect

PORT = 8095
AUTH_PREFIX = "openpc-auth-v1:"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def new_keypair() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return key, base64.b64encode(raw).decode()


async def recv(ws, timeout: float = 15.0, expect: str | None = None):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timeout waiting for {expect or 'frame'}")
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if expect is None or frame.get("type") == expect:
            return frame


async def main(code: str) -> int:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # --- 1. unauthenticated socket must not reach the session -------------
    ws = await connect(f"wss://127.0.0.1:{PORT}", ssl=ctx, max_size=None)
    hello = await recv(ws, expect="host_challenge")
    check(
        "host_challenge carries appliance identity",
        isinstance(hello.get("nonce"), str) and bool(hello.get("appliance", {}).get("certFingerprint")),
        f"appliance={hello.get('appliance', {}).get('name')} "
        f"pin={(hello.get('appliance', {}).get('certFingerprint') or '')[:23]}...",
    )
    check("nonce is a real challenge", len(str(hello.get("nonce"))) >= 16, f"len={len(str(hello.get('nonce')))}")

    await ws.send(json.dumps({"type": "subscribe", "sessionId": "main"}))
    denied = await recv(ws)
    check(
        "unauthenticated subscribe is refused",
        denied.get("type") == "host_error" and "auth" in json.dumps(denied).lower(),
        denied.get("code", ""),
    )

    # --- 2. pair with the one-time code ----------------------------------
    key, public_b64 = new_keypair()
    await ws.send(
        json.dumps(
            {
                "type": "pair",
                "code": code,
                "deviceId": "live-smoke-1",
                "name": "Live Smoke",
                "publicKey": public_b64,
                "kind": "desktop",
            }
        )
    )
    paired = await recv(ws)
    check(
        "pairing code registers the device",
        paired.get("type") == "auth_ok",
        paired.get("type") + " " + str(paired.get("code") or paired.get("deviceId") or ""),
    )
    if paired.get("type") != "auth_ok":
        print("cannot continue without auth:", paired)
        return 1

    # --- 3. ping / subscribe ---------------------------------------------
    await ws.send(json.dumps({"type": "ping"}))
    pong = await recv(ws, expect="pong")
    check("ping/pong", isinstance(pong.get("serverTime"), float))

    await ws.send(json.dumps({"type": "subscribe", "sessionId": "main"}))
    host = await recv(ws, expect="host_hello")
    check(
        "subscribe yields host_hello",
        host.get("sessionId") == "main" and isinstance(host.get("boundary"), int),
        f"state={host.get('state')} hostSeq={host.get('hostSeq')}",
    )

    # --- 4. a full prompt turn -------------------------------------------
    started = time.monotonic()
    await ws.send(json.dumps({"type": "prompt", "id": "req-1", "message": "live smoke says hello"}))
    response = await recv(ws, expect="response")
    check("prompt accepted", response.get("success") is True, str(response.get("command")))

    frames, deltas, text = [], [], ""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic()))
        frames.append(frame.get("type"))
        if frame.get("type") == "message_update":
            event = (frame.get("assistantMessageEvent") or {})
            if event.get("type") == "text_delta":
                deltas.append(event.get("delta", ""))
        if frame.get("type") == "message_end":
            content = frame.get("message", {}).get("content")
            text = content if isinstance(content, str) else json.dumps(content)[:120]
        if frame.get("type") == "agent_end":
            break
    elapsed = time.monotonic() - started
    check("agent_start/agent_end bracket the turn", "agent_start" in frames and frames[-1] == "agent_end", " ".join(frames[:6]))
    check("streamed text deltas arrive", len(deltas) > 0, f"{len(deltas)} deltas: {''.join(deltas)[:60]!r}")
    check("message_end carries the reply", bool(text), str(text)[:60])
    print(f"      turn completed in {elapsed * 1000:.0f} ms")

    # --- 5. session introspection ----------------------------------------
    await ws.send(json.dumps({"type": "session_status", "id": "req-2"}))
    status = await recv(ws, expect="host_session_status")
    check(
        "session_status reports a live child",
        status.get("alive") is True and isinstance(status.get("pid"), int),
        f"pid={status.get('pid')} hostSeq={status.get('hostSeq')}",
    )

    await ws.send(json.dumps({"type": "list_sessions", "id": "req-3"}))
    sessions = await recv(ws, expect="host_sessions")
    check("list_sessions", any(s.get("sessionId") == "main" for s in sessions.get("sessions", [])),
          f"{len(sessions.get('sessions', []))} session(s)")

    # --- 6. reconnect: a registered device can auth without the code ------
    await ws.close()
    ws2 = await connect(f"wss://127.0.0.1:{PORT}", ssl=ctx, max_size=None)
    hello2 = await recv(ws2, expect="host_challenge")
    signature = key.sign(f"{AUTH_PREFIX}{hello2['nonce']}".encode())
    await ws2.send(
        json.dumps(
            {
                "type": "auth",
                "deviceId": "live-smoke-1",
                "nonce": hello2["nonce"],
                "signature": base64.b64encode(signature).decode(),
            }
        )
    )
    reauth = await recv(ws2)
    check("Ed25519 reauth works without the pairing code", reauth.get("type") == "auth_ok", str(reauth.get("code") or ""))

    await ws2.send(json.dumps({"type": "subscribe", "sessionId": "main"}))
    hello3 = await recv(ws2, expect="host_hello")
    check(
        "late joiner gets replay, not an empty session",
        int(hello3.get("messageTotal", 0)) > 0,
        f"messageTotal={hello3.get('messageTotal')} hasMore={hello3.get('hasMore')}",
    )
    await ws2.close()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
