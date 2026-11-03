"""WebSocket edge on :8095 - the only network surface of the appliance.

Wire contract (all frames are single JSON objects, never JSONL):

  server -> client, on connect
    {"type":"host_challenge","nonce","expiresAt","appliance":{"id","name","model","certFingerprint"}}
  client -> server
    {"type":"auth","deviceId","nonce","signature"}          # registered device
    {"type":"pair","code","deviceId","name","publicKey","kind"}  # one-time code
  server -> client
    {"type":"auth_ok","deviceId","applianceId","serverTime"}
  then
    {"type":"subscribe","sessionId":"main"}
  server -> client
    {"type":"host_hello","sessionId","state","messages","messageTotal","hasMore",
     "boundary","engine":{...},"sessions":[...],"localModel":{...}}

After ``host_hello`` every engine frame is forwarded verbatim with two added
cursors: ``sessionId`` and ``hostSeq``. The client ignores frames whose
``hostSeq`` is at or below the ``boundary`` it last received in a snapshot.

Commands are pass-through: any frame with a ``type`` the host does not claim is
written to the child unchanged, keeping the client's own ``id`` so the response
correlates. ``success: true`` means accepted; completion arrives as events.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .auth import (
    AuthManager,
    DeviceRecord,
    DeviceRegistry,
    PairingCodeStore,
    certificate_fingerprint,
    ensure_tls_identity,
    format_fingerprint,
)
from .config import EngineConfig
from .hub import ClientSink, SessionBridge, SessionManager
from .models import ManifestError, ModelSpec, load_default, load_manifest
from .rpc_process import MAX_RECORD_BYTES, EngineError, EngineNotReady

log = logging.getLogger("openpc.ws")

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = MAX_RECORD_BYTES + 65536

#: Host-owned frame types. Everything else with a string ``type`` is forwarded
#: to the engine verbatim.
HOST_FRAME_TYPES = frozenset(
    {
        "auth",
        "pair",
        "subscribe",
        "unsubscribe",
        "resync",
        "ping",
        "list_sessions",
        "new_session",
        "session_status",
    }
)


@dataclass(slots=True)
class _Session:
    """The single session a connection is currently streaming."""

    bridge: SessionBridge
    sink: ClientSink
    boundary: int
    pump: asyncio.Task[None] | None = None


class EngineServer:
    def __init__(
        self,
        config: EngineConfig,
        registry: DeviceRegistry | None = None,
        pairing: PairingCodeStore | None = None,
    ) -> None:
        self.config = config
        self.registry = registry or DeviceRegistry(config.devices_file)
        self.pairing = pairing or PairingCodeStore(ttl=config.pairing_code_ttl, path=config.pairing_file)
        self.auth = AuthManager(self.registry, ttl=config.auth_challenge_ttl)
        self.sessions = SessionManager(config)
        #: The active local model, from the manifest only. A broken manifest
        #: must not stop the edge from serving - ``doctor`` reports it loudly.
        self.local_model: ModelSpec | None = self._load_local_model(config)
        self.appliance_id = config.state_dir.name or "openpc"
        self.fingerprint = certificate_fingerprint(config.tls_cert) if Path(config.tls_cert).exists() else ""
        self._server: Server | None = None
        self.port: int = config.bind_port
        self._connections: set[_Connection] = set()
        #: (sessionId, requestId) -> the connection that issued it. A bridge is
        #: shared by every client watching that session, so the response cannot
        #: be routed from the bridge alone.
        self._routes: dict[tuple[str, str], _Connection] = {}
        self._routers: dict[str, Any] = {}

    @staticmethod
    def _load_local_model(config: EngineConfig) -> ModelSpec | None:
        try:
            manifest = (
                load_manifest(config.model_manifest)
                if config.model_manifest is not None
                else load_default()
            )
        except ManifestError as exc:
            log.warning("model manifest unusable", extra={"detail": str(exc)})
            return None
        if manifest is None:
            return None
        spec = manifest.active
        log.info(
            "local model declared",
            extra={
                "model": spec.key,
                "quant": spec.quant,
                "context": spec.context_length,
                "licence_status": spec.licence.status,
            },
        )
        return spec

    # -- lifecycle ---------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext:
        ensure_tls_identity(self.config.tls_cert, self.config.tls_key)
        self.fingerprint = certificate_fingerprint(self.config.tls_cert)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(certfile=str(self.config.tls_cert), keyfile=str(self.config.tls_key))
        return ctx

    async def start(self) -> None:
        self.config.ensure_dirs()
        self.sessions.start_sweeper()
        self._server = await serve(
            self._handle,
            self.config.bind_host,
            self.config.bind_port,
            ssl=self._ssl_context(),
            max_size=MAX_FRAME_BYTES,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            server_header="openpc-engine",
        )
        with contextlib.suppress(Exception):
            self.port = self._server.sockets[0].getsockname()[1]
        log.info(
            "engine edge listening",
            extra={
                "detail": (
                    f"wss://{self.config.bind_host}:{self.port} "
                    f"pin={format_fingerprint(self.fingerprint)}"
                )
            },
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for conn in list(self._connections):
            with contextlib.suppress(Exception):
                await conn.close("server shutting down")
        await self.sessions.shutdown()

    # -- pass-through response routing -------------------------------------

    def router_for(self, session_id: str) -> Any:
        """Return the bridge-level response router for a session (memoised)."""
        router = self._routers.get(session_id)
        if router is None:

            def route(frame: dict[str, Any]) -> None:
                request_id = frame.get("id")
                if isinstance(request_id, str):
                    conn = self._routes.pop((session_id, request_id), None)
                    if conn is not None:
                        conn.deliver_response(frame)
                        return
                log.debug("unrouted engine response", extra={"session_id": session_id})

            self._routers[session_id] = router = route
        return router

    def register_route(self, session_id: str, request_id: str, conn: _Connection) -> None:
        self._routes[(session_id, request_id)] = conn

    def drop_routes(self, conn: _Connection) -> None:
        for key in [k for k, v in self._routes.items() if v is conn]:
            self._routes.pop(key, None)

    def list_sessions_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "sessionId": b.session_id,
                "alive": b.alive,
                "streaming": b.streaming,
                "clients": b.subscriber_count,
                "hostSeq": b.host_seq,
            }
            for b in self.sessions.sessions()
        ]

    # -- connection --------------------------------------------------------

    async def _handle(self, ws: ServerConnection) -> None:
        conn = _Connection(self, ws)
        self._connections.add(conn)
        try:
            await conn.run()
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("connection handler failed")
        finally:
            self._connections.discard(conn)
            with contextlib.suppress(Exception):
                await conn.shutdown()


class _Connection:
    """One authenticated client connection."""

    def __init__(self, server: EngineServer, ws: ServerConnection) -> None:
        self.server = server
        self.ws = ws
        self.device: DeviceRecord | None = None
        self.session: _Session | None = None
        self._send_lock = asyncio.Lock()
        self._closed = False

    # -- io ----------------------------------------------------------------

    async def send(self, frame: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            async with self._send_lock:
                await self.ws.send(json.dumps(frame, ensure_ascii=True, separators=(",", ":")))
        except ConnectionClosed:
            self._closed = True

    def deliver_response(self, frame: dict[str, Any]) -> None:
        """Called from the engine reader task with a pass-through response."""
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self.send(frame))

    async def send_error(self, code: str, message: str, request_id: str | None = None) -> None:
        frame: dict[str, Any] = {"type": "host_error", "code": code, "message": message}
        if request_id:
            frame["id"] = request_id
        await self.send(frame)

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        if not await self._authenticate():
            return
        assert self.device is not None
        await self.send(
            {
                "type": "auth_ok",
                "protocol": PROTOCOL_VERSION,
                "deviceId": self.device.device_id,
                "applianceId": self.server.appliance_id,
                "serverTime": time.time(),
                "sessions": self.server.list_sessions_payload(),
            }
        )
        await self._command_loop()

    async def shutdown(self) -> None:
        await self.unsubscribe()
        self.server.drop_routes(self)

    async def _authenticate(self) -> bool:
        challenge = self.server.auth.new_challenge()
        await self.send(
            {
                "type": "host_challenge",
                "protocol": PROTOCOL_VERSION,
                "nonce": challenge.nonce,
                "expiresAt": challenge.expires_at,
                "appliance": {
                    "id": self.server.appliance_id,
                    "name": "Open-PC One",
                    "certFingerprint": self.server.fingerprint,
                },
            }
        )
        deadline = asyncio.get_running_loop().time() + self.server.config.auth_challenge_ttl + 5
        while asyncio.get_running_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=self.server.config.auth_challenge_ttl)
            except TimeoutError:
                break
            frame = _decode(raw)
            if frame is None:
                await self.send_error("bad_frame", "frame must be a JSON object")
                continue
            ftype = frame.get("type")
            if ftype == "auth":
                record = self.server.auth.verify(
                    str(frame.get("deviceId", "")),
                    str(frame.get("nonce", "")),
                    str(frame.get("signature", "")),
                )
                if record is None:
                    await self.send_error("auth_failed", "signature rejected")
                    await self.close("auth failed")
                    return False
                self.device = record
                return True
            if ftype == "pair":
                if await self._pair(frame):
                    return True
                continue
            await self.send_error("auth_required", "send auth or pair first")
        await self.close("auth timeout")
        return False

    async def _pair(self, frame: dict[str, Any]) -> bool:
        code = str(frame.get("code", ""))
        if not self.server.pairing.redeem(code):
            # Deliberately vague: do not reveal whether a code existed, expired
            # or was already used.
            await self.send_error("pair_failed", "pairing code is not valid")
            return False
        try:
            record = self.server.registry.add(
                device_id=str(frame.get("deviceId", "")),
                name=str(frame.get("name", "Desktop")),
                public_key_b64=str(frame.get("publicKey", "")),
                kind=str(frame.get("kind", "desktop")),
            )
        except Exception as exc:
            await self.send_error("pair_failed", str(exc))
            return False
        self.device = record
        log.info("device paired", extra={"client_id": record.device_id, "detail": record.kind})
        return True

    # -- commands ----------------------------------------------------------

    async def _command_loop(self) -> None:
        async for raw in self.ws:
            frame = _decode(raw)
            if frame is None:
                await self.send_error("bad_frame", "frame must be a JSON object")
                continue
            ftype = frame.get("type")
            if not isinstance(ftype, str) or not ftype:
                await self.send_error("bad_frame", "missing frame type")
                continue
            try:
                if ftype == "ping":
                    await self.send({"type": "pong", "serverTime": time.time()})
                elif ftype in HOST_FRAME_TYPES:
                    await self._host_command(ftype, frame)
                else:
                    await self._passthrough(frame)
            except ConnectionClosed:
                raise
            except Exception:
                log.exception("frame handling failed", extra={"event": ftype})
                await self.send_error("internal_error", "request failed", frame.get("id"))

    async def _host_command(self, ftype: str, frame: dict[str, Any]) -> None:
        request_id = frame.get("id")
        if ftype == "subscribe":
            session_id = str(frame.get("sessionId") or "main")
            await self.subscribe(session_id)
        elif ftype == "unsubscribe":
            await self.unsubscribe()
        elif ftype == "resync":
            if self.session is None:
                await self.send_error("not_subscribed", "subscribe before resync", request_id)
            else:
                await self._resync(self.session, reason="client_request")
        elif ftype == "list_sessions":
            await self.send(
                {
                    "type": "host_sessions",
                    "id": request_id,
                    "sessions": self.server.list_sessions_payload(),
                }
            )
        elif ftype == "session_status":
            if self.session is None:
                await self.send_error("not_subscribed", "not subscribed", request_id)
            else:
                b = self.session.bridge
                await self.send(
                    {
                        "type": "host_session_status",
                        "id": request_id,
                        "sessionId": b.session_id,
                        "alive": b.alive,
                        "streaming": b.streaming,
                        "hostSeq": b.host_seq,
                        "pid": b.process.pid,
                    }
                )
        elif ftype == "new_session":
            session_id = str(frame.get("sessionId") or f"sess-{secrets.token_hex(4)}")
            if not _valid_session_id(session_id):
                await self.send_error("bad_session_id", "sessionId must be [a-zA-Z0-9._-]{1,64}", request_id)
                return
            await self.subscribe(session_id)
        else:  # auth / pair after authentication
            await self.send_error("already_authenticated", f"{ftype} is not valid after auth", request_id)

    async def _passthrough(self, frame: dict[str, Any]) -> None:
        if self.session is None:
            await self.send_error("not_subscribed", "subscribe before sending commands", frame.get("id"))
            return
        bridge = self.session.bridge
        payload = {k: v for k, v in frame.items() if k not in ("sessionId", "hostSeq")}
        # Remember which client owns each request id so the child's response is
        # delivered back here even though the reader has no internal waiter.
        request_id = payload.get("id")
        if isinstance(request_id, str):
            self.server.register_route(bridge.session_id, request_id, self)
        try:
            await bridge.process.write_frame(payload)
            bridge.heartbeat()
        except EngineNotReady as exc:
            await self.send_error("engine_down", str(exc), request_id)
        except EngineError as exc:
            await self.send_error("engine_error", str(exc), request_id)

    # -- subscription ------------------------------------------------------

    async def subscribe(self, session_id: str) -> None:
        if not _valid_session_id(session_id):
            await self.send_error("bad_session_id", "sessionId must be [a-zA-Z0-9._-]{1,64}")
            return
        await self.unsubscribe()
        try:
            bridge = await self.server.sessions.get(session_id)
        except (EngineError, OSError) as exc:
            log.error("session start failed", extra={"session_id": session_id, "detail": str(exc)})
            await self.send(
                {
                    "type": "host_engine_down",
                    "sessionId": session_id,
                    "reason": str(exc),
                }
            )
            return

        sink = ClientSink(
            client_id=f"{self.device.device_id if self.device else 'anon'}:{secrets.token_hex(3)}",
            queue=asyncio.Queue(maxsize=self.server.config.client_queue_frames),
            max_frames=self.server.config.client_queue_frames,
        )
        bridge.attach(sink)
        bridge.process.on_unmatched_response = self.server.router_for(session_id)
        session = _Session(bridge=bridge, sink=sink, boundary=0)
        self.session = session
        try:
            snap = await bridge.snapshot()
        except (EngineError, OSError) as exc:
            bridge.detach(sink)
            self.session = None
            log.error("snapshot failed", extra={"session_id": session_id, "detail": str(exc)})
            await self.send({"type": "host_engine_down", "sessionId": session_id, "reason": str(exc)})
            return
        session.boundary = int(snap["boundary"])
        await self.send(self._hello_frame(snap, resync=False))
        session.pump = asyncio.create_task(self._pump(session), name=f"pump:{session_id}")
        log.info("client subscribed", extra={"session_id": session_id, "client_id": sink.client_id})

    async def unsubscribe(self) -> None:
        session = self.session
        if session is None:
            return
        self.session = None
        session.bridge.detach(session.sink)
        if session.pump is not None:
            session.pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await session.pump

    def _hello_frame(self, snap: dict[str, Any], *, resync: bool) -> dict[str, Any]:
        frame = {
            "type": "host_hello",
            "resync": resync,
            "sessionId": snap["sessionId"],
            "state": snap["state"],
            "stats": snap.get("stats", {}),
            "messages": snap["messages"],
            "messageTotal": snap["messageTotal"],
            "messageStart": snap["messageStart"],
            "hasMore": snap["hasMore"],
            "boundary": snap["boundary"],
            "engine": snap.get("engine", {}),
            "sessions": self.server.list_sessions_payload(),
        }
        # What the box is *supposed* to be running. A client can show the model
        # and licence status before the first prompt; the engine's own model
        # list remains the runtime truth.
        if self.server.local_model is not None:
            frame["localModel"] = self.server.local_model.as_public_dict()
        return frame

    async def _resync(self, session: _Session, reason: str) -> None:
        bridge = session.bridge
        try:
            snap = await bridge.snapshot()
        except (EngineError, OSError) as exc:
            await self.send({"type": "host_engine_down", "sessionId": bridge.session_id, "reason": str(exc)})
            return
        session.boundary = int(snap["boundary"])
        session.sink.clear_lagged()
        log.info("resynced", extra={"session_id": bridge.session_id, "client_id": session.sink.client_id, "event": reason})
        await self.send(self._hello_frame(snap, resync=True))

    async def _pump(self, session: _Session) -> None:
        """Deliver queued frames in order, resyncing automatically on overflow."""
        try:
            while True:
                frame = await session.sink.queue.get()
                ftype = frame.get("type")
                if ftype == "host_resync_required":
                    await self._resync(session, reason="backpressure")
                    continue
                host_seq = frame.get("hostSeq")
                if isinstance(host_seq, int) and host_seq <= session.boundary:
                    continue  # already contained in the snapshot the client holds
                await self.send(frame)
        except asyncio.CancelledError:
            raise

    async def close(self, reason: str) -> None:
        self._closed = True
        with contextlib.suppress(Exception):
            await self.ws.close(code=1008, reason=reason)


def _decode(raw: str | bytes) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _valid_session_id(session_id: str) -> bool:
    if not session_id or len(session_id) > 64:
        return False
    return all(c.isalnum() or c in "._-" for c in session_id)


async def serve_forever(config: EngineConfig) -> None:
    server = EngineServer(config)
    await server.start()
    try:
        await asyncio.Future()
    finally:
        await server.stop()


__all__ = ["EngineServer", "MAX_FRAME_BYTES", "PROTOCOL_VERSION", "serve_forever"]
