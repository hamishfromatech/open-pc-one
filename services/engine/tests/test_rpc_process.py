"""Child supervision, framing on the pipe, and restart behaviour."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest
from openpc_engine.config import EngineConfig
from openpc_engine.rpc_process import (
    EngineCommandError,
    EngineNotReady,
    EngineProcess,
    probe_engine,
)


async def wait_until(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def test_start_reports_engine_info(config):
    proc = EngineProcess(config, "main")
    try:
        info = await proc.start()
        assert info["contract"] == 1
        assert info["version"].startswith("fake-")
        assert proc.alive
        assert proc.pid is not None
    finally:
        await proc.stop()
    assert not proc.alive


async def test_command_round_trip(config):
    proc = EngineProcess(config, "main")
    try:
        await proc.start()
        state = await proc.command({"type": "get_state"})
        assert state["sessionId"] == "fake-session"
        assert state["cwd"]
        messages = await proc.command({"type": "get_messages"})
        assert messages["messages"] == []
    finally:
        await proc.stop()


async def test_failed_command_raises_with_engine_message(config):
    proc = EngineProcess(config, "main")
    try:
        await proc.start()
        with pytest.raises(EngineCommandError) as exc:
            await proc.command({"type": "not_a_real_command"})
        assert "not_a_real_command" in str(exc.value)
        # A rejected command must not poison the process.
        assert (await proc.command({"type": "get_state"}))["sessionId"] == "fake-session"
    finally:
        await proc.stop()


async def test_events_stream_and_streaming_flag_tracks_the_turn(config):
    events: list[dict] = []
    proc = EngineProcess(config, "main", on_event=events.append)
    try:
        await proc.start()
        data = await proc.command({"type": "prompt", "message": "hello world"})
        assert data == {}  # accepted only; completion arrives as events
        await wait_until(lambda: any(e.get("type") == "agent_end" for e in events))
        types = [e["type"] for e in events]
        assert types[0] == "agent_start"
        assert "message_start" in types
        assert "message_update" in types
        assert types[-1] == "agent_end"
        assert proc.is_streaming is False
        deltas = "".join(
            e["assistantMessageEvent"]["delta"] for e in events if e.get("type") == "message_update"
        )
        assert "echo" in deltas and "hello" in deltas
    finally:
        await proc.stop()


async def test_crashed_child_is_restarted_and_pending_commands_fail(config):
    # Frame 1 (get_state) is answered; frame 2 (the prompt) dies mid-flight.
    # The unattended session policy is covered by its own tests and is turned
    # off here so it does not consume the fake engine's frame budget.
    config.auto_compaction = False
    config.auto_retry = False
    config.engine_args = ("--crash-after", "2")
    proc = EngineProcess(config, "main")
    try:
        await proc.start()
        assert (await proc.command({"type": "get_state"}))["sessionId"] == "fake-session"
        with pytest.raises(EngineNotReady):
            await proc.command({"type": "prompt", "message": "will die"})
        await wait_until(lambda: proc.alive and proc.stats.starts >= 2)
        assert proc.stats.crashes >= 1
        # The restarted child is usable, and its first frame is answered.
        assert (await proc.command({"type": "get_state"}))["sessionId"] == "fake-session"
    finally:
        await proc.stop()


async def test_restart_budget_is_finite(config):
    config.engine_args = ("--crash-immediately",)
    config.restart_max = 1
    proc = EngineProcess(config, "main")
    try:
        await proc.start()
        await wait_until(lambda: proc.stats.crashes >= 2, timeout=20.0)
        await asyncio.sleep(1.5)
        # Budget exhausted: no further spawns, and the LED goes red rather than
        # the box failing silently (ENGINE.md §7).
        assert proc.stats.starts == 2
        assert not proc.alive
        attention = config.run_dir / "attention"
        assert attention.exists()
        assert "restart budget exhausted" in attention.read_text()
    finally:
        await proc.stop()


async def test_start_timeout_surfaces_instead_of_hanging(config):
    config.engine_args = ("--no-handshake",)
    config.engine_start_timeout = 1.0
    proc = EngineProcess(config, "probe")
    with pytest.raises(Exception) as exc:
        await proc.start()
    assert "engine_info" in str(exc.value)
    assert not proc.alive


async def test_continue_is_added_when_a_session_file_exists(config):
    proc = EngineProcess(config, "main")
    assert "--continue" not in proc._argv()
    session_dir = config.session_dir("main")
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "2026-01-01.jsonl").write_text("{}\n")
    assert "--continue" in proc._argv()
    # The dedicated session dir is always passed, never a shared tree.
    argv = proc._argv()
    assert argv[argv.index("--session-dir") + 1] == str(session_dir)


# ---------------------------------------------------------------------------
# Unattended session policy (ENGINE.md §5 auto-compaction, §7 auto-retry)
# ---------------------------------------------------------------------------

SESSION_POLICY = ("set_auto_compaction", "set_auto_retry")


def read_command_log(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def test_unattended_session_policy_is_applied_on_start(config, tmp_path):
    log = tmp_path / "commands.jsonl"
    config.engine_args = ("--command-log", str(log))
    proc = EngineProcess(config, "main")
    try:
        await proc.start()
        await wait_until(lambda: len(read_command_log(log)) >= 2)
        commands = read_command_log(log)
        assert [c["type"] for c in commands[:2]] == list(SESSION_POLICY)
        assert all(c["enabled"] is True for c in commands[:2])
        assert proc.alive
    finally:
        await proc.stop()


async def test_session_policy_is_reapplied_after_a_respawn(config, tmp_path):
    """The setting lives in the child process, so a restart must re-apply it."""
    log = tmp_path / "commands.jsonl"
    config.engine_args = ("--command-log", str(log))
    proc = EngineProcess(config, "main")
    try:
        await proc.start()
        await wait_until(lambda: len(read_command_log(log)) >= 2)
        assert proc._proc is not None
        proc._proc.kill()
        await wait_until(lambda: proc.stats.starts >= 2 and proc.alive)
        await wait_until(lambda: len(read_command_log(log)) >= 4)
        types = [c["type"] for c in read_command_log(log)]
        assert types.count("set_auto_compaction") == 2
        assert types.count("set_auto_retry") == 2
    finally:
        await proc.stop()


async def test_policy_failure_is_logged_but_does_not_kill_the_session(config, caplog):
    config.engine_args = ("--fail-policy",)
    proc = EngineProcess(config, "main")
    try:
        with caplog.at_level(logging.WARNING, logger="openpc.engine.process"):
            await proc.start()
            await wait_until(lambda: "session policy command failed" in caplog.text)
        # The session still works: an older engine without the command must not
        # become an outage.
        assert (await proc.command({"type": "get_state"}))["sessionId"] == "fake-session"
        assert proc.alive
    finally:
        await proc.stop()


async def test_policy_can_be_disabled(config, tmp_path):
    log = tmp_path / "commands.jsonl"
    config.auto_compaction = False
    config.auto_retry = False
    config.engine_args = ("--command-log", str(log))
    proc = EngineProcess(config, "main")
    try:
        await proc.start()
        assert proc.policy_commands() == ()
        await proc.command({"type": "get_state"})
        await asyncio.sleep(0.2)
        assert [c["type"] for c in read_command_log(log)] == ["get_state"]
    finally:
        await proc.stop()


async def test_probe_helper_starts_and_stops(config):
    info = await probe_engine(config, timeout=20.0)
    assert info["contract"] == 1
