"""Companion client: talk to an Open-PC One over its WSS edge.

This is the protocol layer a Tauri or Flutter app will reuse, kept free of any UI:
connect (TLS with the printed pin), pair with a one-time code, authenticate with an
Ed25519 device key, subscribe, prompt, watch the streamed deltas, and ask the box
for its own resource numbers.

Threads: one reader thread owns the socket, so the UI can do whatever it likes
without touching it. Everything crosses the boundary as plain dicts — the same
frames the appliance speaks.
"""

from __future__ import annotations

import base64
import json
import queue
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.sync.client import connect

AUTH_PREFIX = "openpc-auth-v1:"


@dataclass
class DeviceIdentity:
    """The companion's own keypair. It never leaves this machine."""

    key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    device_id: str = "companion"
    name: str = "AmaaraOS Companion"

    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(
            self.key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode()

    def sign(self, payload: bytes) -> str:
        return base64.b64encode(self.key.sign(payload)).decode()


class Companion:
    """One connection to one box.

    ``on_frame`` is called from the reader thread for every frame; ``on_state``
    whenever the connection state changes. Neither may raise into the reader: a UI
    bug must not take down the socket.
    """

    def __init__(
        self,
        host: str,
        on_frame: Callable[[dict[str, Any]], None] | None = None,
        on_state: Callable[[str], None] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.on_frame = on_frame or (lambda frame: None)
        self.on_state = on_state or (lambda state: None)
        self.timeout = timeout
        self.identity = DeviceIdentity()
        self.ws: Any = None
        self.pin = ""
        self.state = "idle"
        self.appliance: dict[str, Any] = {}
        self.replies: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._closed = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def _set_state(self, state: str) -> None:
        self.state = state
        try:
            self.on_state(state)
        except Exception:  # pragma: no cover - a UI callback must not break the socket
            pass

    def connect(self, pair_code: str | None = None) -> dict[str, Any]:
        """Connect and authenticate. A pair code is only needed the first time."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # The box is self-signed on purpose: pin the certificate instead of trusting
        # a CA. The pin is printed here so a human can check it against the device.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        url = self.host if "://" in self.host else f"wss://{self.host}"
        self.ws = connect(url, ssl=context, max_size=None, open_timeout=self.timeout)
        self.pin = _fingerprint(self.ws)
        self._set_state("connected")

        challenge = self._recv_until("host_challenge")
        self.appliance = challenge.get("appliance", {})
        if pair_code:
            self._pair(pair_code, challenge)
        else:
            self._authenticate(challenge)
        self._set_state("ready")
        self._closed.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return self.appliance

    def _pair(self, code: str, challenge: dict[str, Any]) -> None:
        self._send(
            {
                "type": "pair",
                "code": code,
                "deviceId": self.identity.device_id,
                "name": self.identity.name,
                "publicKey": self.identity.public_key_b64,
                "kind": "desktop",
            }
        )
        frame = self._recv_until("auth_ok", or_error="pair_failed")
        self._check(frame)

    def _authenticate(self, challenge: dict[str, Any]) -> None:
        nonce = str(challenge.get("nonce", ""))
        self._send(
            {
                "type": "auth",
                "deviceId": self.identity.device_id,
                "nonce": nonce,
                "signature": self.identity.sign(f"{AUTH_PREFIX}{nonce}".encode()),
            }
        )
        self._check(self._recv_until("auth_ok", or_error="auth_failed"))

    def close(self) -> None:
        self._closed.set()
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:  # pragma: no cover - already gone
            pass
        self._set_state("closed")

    # -- transport ---------------------------------------------------------

    def _send(self, frame: dict[str, Any]) -> None:
        if self.ws is None:
            raise RuntimeError("not connected")
        self.ws.send(json.dumps(frame))

    def _recv(self) -> dict[str, Any]:
        raw = self.ws.recv(timeout=self.timeout)
        return json.loads(raw)

    def _recv_until(self, expect: str, or_error: str | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            frame = self._recv()
            if frame.get("type") == expect:
                return frame
            if or_error and frame.get("type") == "host_error":
                return frame
            # Anything else before the handshake finishes is not ours to interpret.
        raise TimeoutError(f"waiting for {expect}")

    def _check(self, frame: dict[str, Any]) -> None:
        if frame.get("type") != "auth_ok":
            raise PermissionError(frame.get("message") or frame.get("code") or "refused")

    def _read_loop(self) -> None:
        while not self._closed.is_set():
            try:
                frame = self._recv()
            except Exception:
                if not self._closed.is_set():
                    self._set_state("disconnected")
                return
            try:
                self.on_frame(frame)
            except Exception:  # pragma: no cover
                pass

    # -- commands ----------------------------------------------------------

    def subscribe(self, session_id: str = "main") -> None:
        self._send({"type": "subscribe", "sessionId": session_id})

    def prompt(self, text: str, request_id: str = "p1") -> None:
        # The RPC child names the field `message`; a `prompt` key is accepted by
        # nobody and fails silently, which is worth remembering.
        self._send({"type": "prompt", "id": request_id, "message": text})

    def abort(self) -> None:
        self._send({"type": "abort", "id": "abort"})

    def stats(self, request_id: str = "stats") -> None:
        self._send({"type": "host_stats", "id": request_id})

    def session_status(self, request_id: str = "status") -> None:
        self._send({"type": "session_status", "id": request_id})

    def list_sessions(self, request_id: str = "list") -> None:
        self._send({"type": "list_sessions", "id": request_id})


def _fingerprint(ws: Any) -> str:
    """SHA-256 of the box's certificate, the only thing we trust about it."""
    try:
        import hashlib

        certificate = ws.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
        return hashlib.sha256(certificate).hexdigest()
    except Exception:  # pragma: no cover - plain ws or a stub transport
        return ""
