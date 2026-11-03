"""Supervision of one ``a-coder-cli --mode rpc`` child process.

RPC is a single-consumer protocol: exactly one reader may consume the child's
stdout. This module owns that reader and turns the byte stream into frames,
routing:

* ``type == "response"`` with a known ``id``  -> the awaiting ``command()`` call
* everything else                             -> ``on_event`` (live agent stream)

A child that dies is restarted with exponential backoff. Responses that were in
flight at the time of a crash are failed rather than left dangling, because the
protocol has no replay: the client's recovery path is a fresh ``get_state`` +
``get_messages`` snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .config import EngineConfig
from .flags import AttentionFlag
from .jsonl import MAX_RECORD_BYTES, JsonlSplitter, encode_record

log = logging.getLogger("openpc.engine.process")

RPC_CONTRACT = 1

EventCallback = Callable[[dict[str, Any]], None]
ExitCallback = Callable[[str], Awaitable[None]]

READ_CHUNK = 65536


class EngineError(RuntimeError):
    """The child could not be started, or the protocol was violated."""


class EngineNotReady(EngineError):
    """No live child process; the command was not sent."""


class EngineCommandError(EngineError):
    """The engine answered ``success: false``."""

    def __init__(self, command: str, response: dict[str, Any]) -> None:
        self.command = command
        self.response = response
        super().__init__(response.get("error") or f"{command} failed")


@dataclass(slots=True)
class ProcessStats:
    starts: int = 0
    crashes: int = 0
    frames_in: int = 0
    commands_out: int = 0
    last_exit_code: int | None = None
    last_crash_at: float | None = None
    stderr_tail: list[str] = field(default_factory=list)


class EngineProcess:
    """One session's supervised RPC child."""

    def __init__(
        self,
        config: EngineConfig,
        session_id: str,
        on_event: EventCallback | None = None,
        on_exit: ExitCallback | None = None,
    ) -> None:
        self.config = config
        self.session_id = session_id
        self._on_event = on_event
        self._on_exit = on_exit

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        #: Bootstrap commands applied to every child after its handshake. Kept
        #: as tasks so the stdout reader never blocks waiting for its own
        #: responses (that would deadlock the pipe).
        self._policy_tasks: set[asyncio.Task[None]] = set()
        self._ready = asyncio.Event()
        self._stopping = False
        self._paused = False
        #: Called with a `response` frame that no internal `command()` is
        #: waiting for. The WebSocket edge uses this to hand a client's own
        #: request/response pair straight back to that client.
        self.on_unmatched_response: Callable[[dict[str, Any]], None] | None = None

        self.engine_info: dict[str, Any] = {}
        self.stats = ProcessStats()
        self.is_streaming = False
        self.last_activity = time.monotonic()

    # -- lifecycle ---------------------------------------------------------

    async def start(self, timeout: float | None = None) -> dict[str, Any]:
        """Start supervising and wait for the first ``engine_info`` handshake."""
        if self._supervisor is not None:
            return self.engine_info
        self._stopping = False
        self._supervisor = asyncio.create_task(self._supervise(), name=f"supervise:{self.session_id}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout or self.config.engine_start_timeout)
        except TimeoutError as exc:
            await self.stop()
            raise EngineError(
                f"engine did not emit engine_info within {timeout or self.config.engine_start_timeout}s"
            ) from exc
        return self.engine_info

    async def stop(self, graceful_timeout: float = 5.0) -> None:
        """Stop the child and its supervisor. Idempotent."""
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), graceful_timeout)
            except TimeoutError:
                proc.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), 3.0)
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        policy_tasks = list(self._policy_tasks)
        for task in policy_tasks:
            task.cancel()
        self._policy_tasks.clear()
        if policy_tasks:
            await asyncio.gather(*policy_tasks, return_exceptions=True)
        self._reject_pending(EngineNotReady("engine session stopped"))

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and self._ready.is_set()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def pause_events(self) -> None:
        """Stop handing events to ``on_event`` (used during snapshot replay)."""
        self._paused = True

    def resume_events(self) -> None:
        self._paused = False

    # -- commands ----------------------------------------------------------

    async def command(self, command: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        """Send one command and await its ``response`` frame.

        The engine's ``success: true`` only means *accepted*; post-acceptance
        failures arrive as events, never as a second response. Callers that need
        completion must watch the event stream (or poll ``get_state``).
        """
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None or not self._ready.is_set():
            raise EngineNotReady("engine process is not running")

        cmd_id = str(uuid.uuid4())
        payload = {"id": cmd_id, **command}
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[cmd_id] = future
        try:
            proc.stdin.write(encode_record(payload))
            await proc.stdin.drain()
            self.stats.commands_out += 1
            self.last_activity = time.monotonic()
            response = await asyncio.wait_for(future, timeout)
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise EngineNotReady(f"engine stdin closed: {exc}") from exc
        finally:
            self._pending.pop(cmd_id, None)

        if not response.get("success"):
            raise EngineCommandError(command.get("type", "?"), response)
        return response.get("data", {}) if "data" in response else {}

    async def send(self, command: dict[str, Any]) -> dict[str, Any]:
        """Send a command and return the whole response frame."""
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None or not self._ready.is_set():
            raise EngineNotReady("engine process is not running")
        cmd_id = str(uuid.uuid4())
        payload = {"id": cmd_id, **command}
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[cmd_id] = future
        try:
            proc.stdin.write(encode_record(payload))
            await proc.stdin.drain()
            self.stats.commands_out += 1
            self.last_activity = time.monotonic()
            return await asyncio.wait_for(future, 60.0)
        finally:
            self._pending.pop(cmd_id, None)

    def notify_activity(self) -> None:
        self.last_activity = time.monotonic()

    # -- session policy ----------------------------------------------------

    def policy_commands(self) -> tuple[dict[str, Any], ...]:
        """Bootstrap commands that make an unattended session safe.

        ENGINE.md §5 (auto-compaction) and §7 (auto-retry). Both are *our*
        defaults, not the user's: an appliance that runs for months cannot rely
        on a human noticing a full context window or a transient provider
        error. The commands are idempotent, so applying them on every respawn
        is harmless.
        """
        commands: list[dict[str, Any]] = []
        if self.config.auto_compaction:
            commands.append({"type": "set_auto_compaction", "enabled": True})
        if self.config.auto_retry:
            commands.append({"type": "set_auto_retry", "enabled": True})
        return tuple(commands)

    def _schedule_policy(self) -> None:
        if not self.policy_commands():
            return
        task = asyncio.create_task(self._apply_session_policy(), name=f"policy:{self.session_id}")
        self._policy_tasks.add(task)
        task.add_done_callback(self._policy_tasks.discard)

    async def _apply_session_policy(self) -> None:
        """Best-effort: an older engine that lacks a command must not take the
        session down, but the failure has to be visible in journald."""
        for command in self.policy_commands():
            try:
                await self.command(command, timeout=self.config.policy_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - policy must never be fatal
                log.warning(
                    "session policy command failed",
                    extra={
                        "session_id": self.session_id,
                        "detail": f"{command.get('type')}: {exc}",
                    },
                )

    async def write_frame(self, frame: dict[str, Any]) -> None:
        """Forward a client-supplied frame untouched, preserving its ``id``.

        Used for pass-through RPC commands. The child's ``response`` comes back
        through :attr:`on_unmatched_response`, not through ``command()``.
        """
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None or not self._ready.is_set():
            raise EngineNotReady("engine process is not running")
        proc.stdin.write(encode_record(frame))
        await proc.stdin.drain()
        self.stats.commands_out += 1
        self.last_activity = time.monotonic()

    # -- supervision -------------------------------------------------------

    def _argv(self) -> list[str]:
        session_dir = self.config.session_dir(self.session_id)
        argv = [
            *self.config.engine_command,
            "--mode",
            "rpc",
            "--session-dir",
            str(session_dir),
            *self.config.engine_args,
        ]
        # Resume rather than fork a blank conversation after an idle reap or a
        # crash. Each appliance session owns a dedicated --session-dir, so there
        # is exactly one CLI session to continue.
        if any(session_dir.glob("*.jsonl")):
            argv.append("--continue")
        return argv

    async def _supervise(self) -> None:
        backoff = 1.0
        consecutive = 0
        while not self._stopping:
            try:
                await self._spawn_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.error("spawn failed", extra={"session_id": self.session_id, "detail": str(exc)})
                self.stats.crashes += 1
            if self._stopping:
                return

            code = self._proc.returncode if self._proc is not None else None
            self.stats.last_exit_code = code
            self.stats.last_crash_at = time.monotonic()
            self.stats.crashes += 1
            consecutive += 1
            log.warning(
                "engine child exited",
                extra={"session_id": self.session_id, "detail": f"code={code} consecutive={consecutive}"},
            )
            self._ready.clear()
            self.is_streaming = False
            self._reject_pending(EngineNotReady(f"engine exited (code {code})"))

            if self._on_exit is not None:
                with contextlib.suppress(Exception):
                    await self._on_exit("exited")

            if consecutive > self.config.restart_max:
                log.error(
                    "restart budget exhausted; session left down",
                    extra={"session_id": self.session_id, "detail": f"{consecutive} consecutive exits"},
                )
                # ENGINE.md §7: a hard stop after N failures, with the LED going
                # red. Silence is the worst failure mode on a screenless box.
                AttentionFlag(self.config.run_dir).raise_flag(
                    f"engine restart budget exhausted for session {self.session_id} "
                    f"({consecutive} consecutive exits, last code {code})"
                )
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _spawn_once(self) -> None:
        argv = self._argv()
        cwd = str(self.config.agent_cwd)
        os.makedirs(cwd, exist_ok=True)
        os.makedirs(self.config.session_dir(self.session_id), exist_ok=True)

        log.info(
            "spawning engine child",
            extra={"session_id": self.session_id, "detail": " ".join(argv)},
        )
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_RECORD_BYTES + READ_CHUNK,
            start_new_session=True,  # give the agent its own process group
        )
        self.stats.starts += 1
        self._reader_task = asyncio.create_task(self._read_stdout(), name=f"stdout:{self.session_id}")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name=f"stderr:{self.session_id}")

        await self._proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5.0)

    async def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        splitter = JsonlSplitter()
        try:
            while True:
                chunk = await proc.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                for line in splitter.feed(chunk):
                    self._handle_line(line, proc)
            for line in splitter.finish():
                self._handle_line(line, proc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.error("stdout reader failed", extra={"session_id": self.session_id, "detail": str(exc)})

    async def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    break
                for raw in chunk.decode("utf-8", errors="replace").splitlines():
                    if not raw.strip():
                        continue
                    self.stats.stderr_tail.append(raw)
                    del self.stats.stderr_tail[:-200]
                    log.debug("engine stderr", extra={"session_id": self.session_id, "detail": raw})
        except asyncio.CancelledError:
            raise

    def _handle_line(self, line: str, proc: asyncio.subprocess.Process) -> None:
        import json as _json

        if not line.strip():
            return
        try:
            frame = _json.loads(line)
        except _json.JSONDecodeError:
            log.warning("dropping malformed JSONL frame", extra={"session_id": self.session_id})
            return
        if not isinstance(frame, dict):
            log.warning("dropping non-object frame", extra={"session_id": self.session_id})
            return

        self.stats.frames_in += 1
        self.last_activity = time.monotonic()

        frame_type = frame.get("type")

        if frame_type == "engine_info":
            contract = frame.get("contract")
            if contract != RPC_CONTRACT:
                log.error(
                    "unsupported RPC contract; refusing to bridge",
                    extra={"session_id": self.session_id, "detail": f"engine={contract} bridge={RPC_CONTRACT}"},
                )
            self.engine_info = frame
            self._ready.set()
            # Applied on every handshake, including after a respawn: the child
            # is a new process and starts with the CLI's own defaults again.
            self._schedule_policy()
            return

        if frame_type == "response":
            cmd_id = frame.get("id")
            if cmd_id and cmd_id in self._pending:
                future = self._pending.pop(cmd_id)
                if not future.done():
                    future.set_result(frame)
                return
            # A response with no internal waiter: either a client's own
            # pass-through command, or a command that timed out. Hand it to the
            # edge, which knows which client issued the id.
            if self.on_unmatched_response is not None:
                try:
                    self.on_unmatched_response(frame)
                except Exception:  # pragma: no cover - defensive
                    log.exception("unmatched-response callback failed", extra={"session_id": self.session_id})
            else:
                log.debug("orphan response frame", extra={"session_id": self.session_id})
            return

        if frame_type == "agent_start":
            self.is_streaming = True
        elif frame_type == "agent_end":
            self.is_streaming = False

        if self._paused:
            return
        if self._on_event is not None:
            try:
                self._on_event(frame)
            except Exception:  # pragma: no cover - subscriber must not kill reader
                log.exception("event callback failed", extra={"session_id": self.session_id})

    def _reject_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


async def probe_engine(config: EngineConfig, timeout: float = 60.0) -> dict[str, Any]:
    """Start one child, read ``engine_info``, stop it. Used by the CLI doctor."""
    proc = EngineProcess(config, session_id="probe")
    try:
        return await proc.start(timeout=timeout)
    finally:
        await proc.stop()
