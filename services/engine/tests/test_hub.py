"""Fan-out, backpressure, replay boundaries and idle reaping."""

from __future__ import annotations

import asyncio
import time

import pytest
from openpc_engine.hub import ClientSink, SessionBridge, SessionManager
from openpc_engine.rpc_process import EngineNotReady


async def wait_until(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def make_sink(max_frames: int = 64, client_id: str = "c1") -> ClientSink:
    return ClientSink(client_id=client_id, queue=asyncio.Queue(maxsize=max_frames), max_frames=max_frames)


async def drain(sink: ClientSink) -> list[dict]:
    out = []
    while not sink.queue.empty():
        out.append(sink.queue.get_nowait())
    return out


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------


async def test_sink_forwards_in_order_until_full():
    sink = make_sink(2)
    assert sink.offer({"hostSeq": 1}) is True
    assert sink.offer({"hostSeq": 2}) is True
    assert sink.offer({"hostSeq": 3}) is False  # full -> drop, mark lagged
    frames = await drain(sink)
    assert sink.lagged is True
    assert frames[-1]["type"] == "host_resync_required"
    assert frames[-1]["reason"] == "backpressure"
    # While lagged everything is dropped, so memory cannot grow.
    assert sink.offer({"hostSeq": 4}) is False
    assert sink.queue.empty()


async def test_sink_recovers_after_resync():
    sink = make_sink(1)
    sink.offer({"hostSeq": 1})
    sink.offer({"hostSeq": 2})
    await drain(sink)
    sink.clear_lagged()
    assert sink.offer({"hostSeq": 9}) is True
    assert (await drain(sink))[0]["hostSeq"] == 9


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


async def test_two_clients_receive_the_same_ordered_frames(config):
    bridge = SessionBridge(config, "main")
    try:
        await bridge.start()
        one, two = make_sink(client_id="a"), make_sink(client_id="b")
        bridge.attach(one)
        bridge.attach(two)
        assert bridge.subscriber_count == 2

        await bridge.process.command({"type": "prompt", "message": "hi"})
        await wait_until(lambda: not one.queue.empty() and not two.queue.empty())
        await asyncio.sleep(0.2)

        frames_one, frames_two = await drain(one), await drain(two)
        seq_one = [f["hostSeq"] for f in frames_one if "hostSeq" in f]
        seq_two = [f["hostSeq"] for f in frames_two if "hostSeq" in f]
        assert seq_one == seq_two
        assert seq_one == sorted(seq_one)
        assert len(set(seq_one)) == len(seq_one)
    finally:
        await bridge.stop()


async def test_forwarded_frame_is_flat_with_cursors(config):
    bridge = SessionBridge(config, "main")
    try:
        await bridge.start()
        sink = make_sink()
        bridge.attach(sink)
        await bridge.process.command({"type": "prompt", "message": "hi"})
        await wait_until(lambda: not sink.queue.empty())
        await asyncio.sleep(0.2)
        frames = await drain(sink)
        start = next(f for f in frames if f.get("type") == "agent_start")
        assert start["sessionId"] == "main"
        assert isinstance(start["hostSeq"], int)
        assert isinstance(start["engineSeq"], int)
        # The engine's own per-process `seq` is normalised away.
        assert "seq" not in start
    finally:
        await bridge.stop()


async def test_engine_exit_notifies_clients(config):
    # Policy frames are covered in test_rpc_process; here they would consume the
    # fake engine's crash-after budget before the client's own command.
    config.auto_compaction = False
    config.auto_retry = False
    config.engine_args = ("--crash-after", "1")
    bridge = SessionBridge(config, "main")
    try:
        await bridge.start()
        sink = make_sink()
        bridge.attach(sink)
        with pytest.raises(EngineNotReady):
            await bridge.process.command({"type": "get_state"})
        await wait_until(
            lambda: any(f.get("type") == "host_engine_down" for f in sink.queue._queue)  # type: ignore[attr-defined]
        )
    finally:
        await bridge.stop()


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


async def test_snapshot_carries_state_messages_and_boundary(config):
    manager = SessionManager(config)
    try:
        bridge = await manager.get("main")
        empty = await bridge.snapshot()
        assert empty["state"]["sessionId"] == "fake-session"
        assert empty["messages"] == []
        assert empty["boundary"] == bridge.host_seq

        await bridge.process.command({"type": "prompt", "message": "remember this"})
        await wait_until(lambda: bridge.streaming is False and bridge.host_seq > empty["boundary"])
        await asyncio.sleep(0.1)

        snap = await bridge.snapshot()
        # get_session_snapshot also carries context usage in one round trip.
        assert snap["stats"]["contextUsage"]["contextWindow"] == 262144
        assert [m["role"] for m in snap["messages"]] == ["user", "assistant"]
        assert snap["messages"][0]["content"] == "remember this"
        # Boundary is taken after both snapshot responses, so a replaying client
        # can discard buffered frames at or below it without losing anything.
        assert snap["boundary"] >= empty["boundary"]
    finally:
        await manager.shutdown()


async def test_snapshot_page_is_bounded(config):
    config.replay_limit = 2
    manager = SessionManager(config)
    try:
        bridge = await manager.get("main")
        for i in range(4):
            await bridge.process.command({"type": "prompt", "message": f"turn {i}"})
            await wait_until(lambda: bridge.streaming is False)
        snap = await bridge.snapshot()
        assert len(snap["messages"]) <= 2
        assert snap["messageTotal"] == 8
        assert snap["hasMore"] is True
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------
# Session lifetime
# ---------------------------------------------------------------------------


async def test_idle_session_with_no_clients_is_reaped(config):
    manager = SessionManager(config)
    try:
        bridge = await manager.get("main")
        assert bridge.alive
        reaped = await manager.sweep_once(now=time.monotonic() + config.idle_timeout + 1)
        assert reaped == ["main"]
        assert await manager.get_existing("main") is None
    finally:
        await manager.shutdown()


async def test_session_with_a_client_is_never_reaped(config):
    manager = SessionManager(config)
    try:
        bridge = await manager.get("main")
        bridge.attach(make_sink())
        reaped = await manager.sweep_once(now=time.monotonic() + config.idle_timeout + 1)
        assert reaped == []
        assert bridge.alive
    finally:
        await manager.shutdown()


async def test_streaming_session_is_never_reaped(config):
    manager = SessionManager(config)
    try:
        bridge = await manager.get("main")
        # Simulate a turn in flight: the reaper must not kill work in progress.
        bridge.process.is_streaming = True
        reaped = await manager.sweep_once(now=time.monotonic() + config.idle_timeout + 1)
        assert reaped == []
        assert bridge.alive
    finally:
        await manager.shutdown()


async def test_same_session_id_returns_the_same_bridge(config):
    manager = SessionManager(config)
    try:
        one = await manager.get("main")
        two = await manager.get("main")
        assert one is two
        assert len(manager.sessions()) == 1
        assert await manager.close_session("main") is True
        assert await manager.close_session("main") is False
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------
# LED integration
# ---------------------------------------------------------------------------


def test_activity_flag_follows_turn_events(config):
    """The LED service reads this file; the engine owns it."""
    flag = config.run_dir / "engine-busy"
    bridge = SessionBridge(config, "main")
    assert not flag.exists()
    bridge._on_engine_event({"type": "agent_start"})
    assert flag.exists()
    bridge._on_engine_event({"type": "message_update"})
    assert flag.exists()  # mid-turn frames do not clear it
    bridge._on_engine_event({"type": "agent_end"})
    assert not flag.exists()


async def test_activity_flag_is_cleared_when_a_session_stops(config):
    flag = config.run_dir / "engine-busy"
    bridge = SessionBridge(config, "main")
    bridge._on_engine_event({"type": "agent_start"})
    assert flag.exists()
    await bridge.stop()
    assert not flag.exists()
