"""Fan-out, replay and session lifetime for the WebSocket edge.

RPC is single-consumer, so exactly one :class:`SessionBridge` reads each child
process and copies its frames to every attached client. Three properties matter:

**Ordering.** The bridge stamps its own monotonic ``hostSeq`` on every frame. The
engine's ``seq`` restarts at 1 whenever a child is respawned, so it cannot be
used as a client-side cursor; ``hostSeq`` never goes backwards for a session
while the engine process is alive.

**Backpressure.** A client that stops reading must never grow the bridge's
memory. Each client owns a bounded queue. On overflow the queue is dropped, the
client is marked lagged, and it receives one ``host_resync_required`` frame. It
then asks for ``resync`` and gets a fresh snapshot. Frames are dropped, never
reordered, so a client that ignores the signal is merely stale, not corrupt.

**Late joiners.** On attach the bridge asks the child for ``get_state`` and a
page of ``get_messages``, emits them as ``host_hello``, then releases the frames
buffered while the snapshot was being taken, skipping anything already covered
by the snapshot boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import EngineConfig
from .flags import ActivityFlag
from .rpc_process import EngineCommandError, EngineProcess

log = logging.getLogger("openpc.hub")

#: Client commands the host handles itself; everything else is passed to the
#: engine verbatim. Kept explicit so a typo cannot silently reach the child.
HOST_COMMANDS = frozenset(
    {"subscribe", "unsubscribe", "resync", "ping", "host_status", "list_sessions", "new_session"}
)


@dataclass(slots=True, eq=False)
class ClientSink:
    """A bounded, ordered delivery queue for one connected client.

    Identity hashing (``eq=False``): a sink is a handle, not a value. Two sinks
    for the same device are different subscribers and must never collapse.
    """

    client_id: str
    queue: asyncio.Queue[dict[str, Any]]
    max_frames: int
    lagged: bool = False
    attached_at: float = field(default_factory=time.monotonic)
    resyncs: int = 0

    def offer(self, frame: dict[str, Any]) -> bool:
        """Enqueue a frame. Returns False if the frame was dropped."""
        if self.lagged:
            return False
        try:
            self.queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            # Drop the backlog and ask for a snapshot instead of growing memory
            # or shipping a torn sequence to the client.
            while not self.queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.queue.get_nowait()
            self.lagged = True
            self.resyncs += 1
            log.warning(
                "client lagged; dropped backlog",
                extra={"client_id": self.client_id, "detail": f"resyncs={self.resyncs}"},
            )
            self.queue.put_nowait(
                {
                    "type": "host_resync_required",
                    "reason": "backpressure",
                    "clientId": self.client_id,
                }
            )
            return False

    def clear_lagged(self) -> None:
        self.lagged = False


class SessionBridge:
    """One supervised engine child plus the set of clients watching it."""

    def __init__(self, config: EngineConfig, session_id: str) -> None:
        self.config = config
        self.session_id = session_id
        self.process = EngineProcess(
            config,
            session_id,
            on_event=self._on_engine_event,
            on_exit=self._on_engine_exit,
        )
        self._sinks: set[ClientSink] = set()
        self._host_seq = 0
        self._lock = asyncio.Lock()
        self.activity = ActivityFlag(config.run_dir)
        self.created_at = time.monotonic()
        self.last_snapshot: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        return await self.process.start()

    async def stop(self) -> None:
        self.activity.clear()
        await self.process.stop()

    @property
    def alive(self) -> bool:
        return self.process.alive

    @property
    def streaming(self) -> bool:
        return self.process.is_streaming

    @property
    def subscriber_count(self) -> int:
        return len(self._sinks)

    @property
    def sinks(self) -> tuple[ClientSink, ...]:
        """Snapshot of attached clients (diagnostics and tests)."""
        return tuple(self._sinks)

    @property
    def last_activity(self) -> float:
        return self.process.last_activity

    @property
    def host_seq(self) -> int:
        return self._host_seq

    def heartbeat(self) -> None:
        """Refresh idle timer without touching the engine (client is alive)."""
        self.process.notify_activity()

    # -- fan-out -----------------------------------------------------------

    def attach(self, sink: ClientSink) -> None:
        self._sinks.add(sink)

    def detach(self, sink: ClientSink) -> None:
        self._sinks.discard(sink)

    def _on_engine_event(self, frame: dict[str, Any]) -> None:
        # Mirror turn state into the flag the LED service watches. Cheap, and it
        # is the only channel the LED needs from the engine.
        if frame.get("type") == "agent_start":
            self.activity.set_busy(True)
        elif frame.get("type") == "agent_end":
            self.activity.set_busy(False)
        self._host_seq += 1
        # Flatten rather than nest: the wire frame is exactly the engine frame
        # plus the two cursors. Engine frames carry no `hostSeq`, and their own
        # `sessionId` always equals ours, so neither key is ambiguous.
        out = {**frame, "sessionId": self.session_id, "hostSeq": self._host_seq, "engineSeq": frame.get("seq")}
        out.pop("seq", None)
        for sink in list(self._sinks):
            sink.offer(out)

    async def _on_engine_exit(self, reason: str) -> None:
        self._host_seq += 1
        out = {
            "type": "host_engine_down",
            "sessionId": self.session_id,
            "hostSeq": self._host_seq,
            "reason": reason,
        }
        for sink in list(self._sinks):
            sink.offer(out)

    # -- replay ------------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        """State + stats + one page of history from the child.

        ``get_session_snapshot`` is the documented one-activation round trip
        (it also carries ``contextUsage``), so it is the primary call; a plain
        ``get_state`` is the fallback for an older engine. Everything is
        answered on the same single-consumer stdout stream, so any event frame
        the bridge has already forwarded is reflected in the result.
        ``boundary`` is the ``hostSeq`` at that moment: frames at or below it
        must be skipped by a replaying client.
        """
        stats: dict[str, Any] = {}
        try:
            combined = await self.process.command({"type": "get_session_snapshot"}, timeout=60.0)
            state = combined.get("state", {})
            stats = combined.get("stats", {})
            if not state:
                raise EngineCommandError("get_session_snapshot", {"error": "empty state"})
        except EngineCommandError:
            state = await self.process.command({"type": "get_state"}, timeout=30.0)
        limit = self.config.replay_limit
        page: dict[str, Any] = {"messages": [], "total": 0, "start": 0, "end": 0, "hasMore": False}
        try:
            page = await self.process.command({"type": "get_messages", "limit": limit}, timeout=120.0)
        except EngineCommandError as exc:
            log.warning("get_messages failed during replay", extra={"session_id": self.session_id, "detail": str(exc)})
        boundary = self._host_seq
        snap = {
            "sessionId": self.session_id,
            "state": state,
            "stats": stats,
            "messages": page.get("messages", []),
            "messageTotal": page.get("total", len(page.get("messages", []))),
            "messageStart": page.get("start", 0),
            "hasMore": page.get("hasMore", False),
            "boundary": boundary,
            "engine": self.process.engine_info,
        }
        self.last_snapshot = snap
        return snap


class SessionManager:
    """Owns every session's child process and reaps idle ones."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self._bridges: dict[str, SessionBridge] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None

    async def get(self, session_id: str, *, start: bool = True) -> SessionBridge:
        async with self._lock:
            bridge = self._bridges.get(session_id)
            if bridge is None:
                bridge = SessionBridge(self.config, session_id)
                self._bridges[session_id] = bridge
            if start and not bridge.alive:
                await bridge.start()
            return bridge

    async def get_existing(self, session_id: str) -> SessionBridge | None:
        return self._bridges.get(session_id)

    def sessions(self) -> list[SessionBridge]:
        return list(self._bridges.values())

    async def close_session(self, session_id: str) -> bool:
        async with self._lock:
            bridge = self._bridges.pop(session_id, None)
        if bridge is None:
            return False
        await bridge.stop()
        return True

    # -- idle reaper -------------------------------------------------------

    def start_sweeper(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop(), name="idle-sweeper")

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.config.idle_sweep_interval)
                await self.sweep_once()
        except asyncio.CancelledError:
            raise

    async def sweep_once(self, now: float | None = None) -> list[str]:
        """Stop sessions with no clients, no turn in flight, past the idle TTL.

        The session JSONL on disk is the source of truth, so killing the child
        loses nothing: the next client gets a fresh child resumed with
        ``--continue`` and a replayed snapshot.
        """
        now = now if now is not None else time.monotonic()
        reaped: list[str] = []
        for bridge in list(self._bridges.values()):
            if bridge.subscriber_count > 0 or bridge.streaming:
                continue
            if now - bridge.last_activity < self.config.idle_timeout:
                continue
            async with self._lock:
                if self._bridges.pop(bridge.session_id, None) is None:
                    continue
            await bridge.stop()
            reaped.append(bridge.session_id)
            log.info("idle session reaped", extra={"session_id": bridge.session_id})
        return reaped

    async def shutdown(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        for bridge in list(self._bridges.values()):
            with contextlib.suppress(Exception):
                await bridge.stop()
        self._bridges.clear()
