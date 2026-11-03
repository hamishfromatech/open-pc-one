"""End-to-end: TLS, auth, subscribe, replay, streaming, backpressure resync.

Everything runs against ``fake_engine.py`` - no LLM, no provider auth, no
network beyond 127.0.0.1.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpc_engine.models import load_manifest
from openpc_engine.ws_server import EngineServer
from wsclient import WssClient

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
async def make_server(config):
    servers: list[EngineServer] = []

    async def make(cfg=None) -> EngineServer:
        srv = EngineServer(cfg or config)
        await srv.start()
        servers.append(srv)
        return srv

    yield make
    for srv in servers:
        await srv.stop()


async def _connected(make_server, config, registry, device_key) -> tuple[EngineServer, WssClient]:
    key, public_b64 = device_key
    registry.add("phone-1", "Pixel", public_b64, kind="mobile")
    srv = await make_server()
    client = WssClient(srv.port, "phone-1", key)
    await client.connect()
    return srv, client


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


async def test_challenge_is_issued_before_anything_else(make_server, config, registry, device_key):
    srv = await make_server()
    key, _ = device_key
    client = WssClient(srv.port, "phone-1", key)
    challenge = await client.connect(authenticate=False)
    assert challenge["protocol"] == 1
    assert challenge["appliance"]["certFingerprint"] == srv.fingerprint
    assert challenge["nonce"]
    await client.close()


async def test_unauthenticated_client_cannot_send_commands(make_server, config, registry, device_key):
    srv = await make_server()
    key, _ = device_key
    client = WssClient(srv.port, "phone-1", key)
    await client.connect(authenticate=False)
    await client.send({"type": "prompt", "message": "let me in"})
    error = await client.recv(expect="host_error")
    assert error["code"] == "auth_required"
    await client.close()


async def test_registered_device_authenticates(make_server, config, registry, device_key):
    srv = await make_server()
    key, public_b64 = device_key
    srv.registry.add("phone-1", "Pixel", public_b64, kind="mobile")
    client = WssClient(srv.port, "phone-1", key)
    ok = await client.connect()
    assert ok["deviceId"] == "phone-1"
    assert ok["applianceId"]
    assert ok["protocol"] == 1
    await client.close()


async def test_unknown_device_is_rejected_and_disconnected(make_server, config, registry, device_key):
    srv = await make_server()
    key, _ = device_key
    client = WssClient(srv.port, "not-registered", key)
    challenge = await client.connect(authenticate=False)
    from openpc_engine.auth import AUTH_PREFIX, sign_with

    await client.send(
        {
            "type": "auth",
            "deviceId": "not-registered",
            "nonce": challenge["nonce"],
            "signature": sign_with(key, f"{AUTH_PREFIX}{challenge['nonce']}".encode()),
        }
    )
    error = await client.recv(expect="host_error")
    assert error["code"] == "auth_failed"
    await client.close()


async def test_paired_device_is_registered_and_authenticated(make_server, config, registry, device_key):
    srv = await make_server()
    key, public_b64 = device_key
    code, _expires = srv.pairing.mint()
    client = WssClient(srv.port, "laptop-1", key)
    await client.connect(authenticate=False)
    await client.send(
        {
            "type": "pair",
            "code": code,
            "deviceId": "laptop-1",
            "name": "MacBook",
            "publicKey": public_b64,
            "kind": "desktop",
        }
    )
    ok = await client.recv(expect="auth_ok")
    assert ok["deviceId"] == "laptop-1"
    assert srv.registry.get("laptop-1").kind == "desktop"  # type: ignore[union-attr]
    # The code is burnt: a second attempt must not work.
    assert srv.pairing.redeem(code) is False
    await client.close()


async def test_bad_pairing_code_is_refused(make_server, config, registry, device_key):
    srv = await make_server()
    key, public_b64 = device_key
    client = WssClient(srv.port, "laptop-1", key)
    await client.connect(authenticate=False)
    await client.send({"type": "pair", "code": "NOPENOPE", "deviceId": "l", "publicKey": public_b64})
    error = await client.recv(expect="host_error")
    assert error["code"] == "pair_failed"
    await client.close()


# ---------------------------------------------------------------------------
# Subscribe / replay
# ---------------------------------------------------------------------------


async def test_subscribe_returns_a_full_snapshot(make_server, config, registry, device_key):
    srv, client = await _connected(make_server, config, registry, device_key)
    hello = await client.subscribe("main")
    assert hello["sessionId"] == "main"
    assert hello["messages"] == []
    assert hello["state"]["sessionId"] == "fake-session"
    assert hello["engine"]["contract"] == 1
    assert isinstance(hello["boundary"], int)
    await client.close()


async def test_commands_before_subscribe_are_refused(make_server, config, registry, device_key):
    srv, client = await _connected(make_server, config, registry, device_key)
    await client.send({"type": "prompt", "message": "early"})
    error = await client.recv(expect="host_error")
    assert error["code"] == "not_subscribed"
    await client.close()


async def test_prompt_streams_events_and_preserves_the_request_id(make_server, config, registry, device_key):
    srv, client = await _connected(make_server, config, registry, device_key)
    await client.subscribe("main")
    await client.send({"type": "prompt", "message": "hello", "id": "req-42"})

    response = await client.recv_until(lambda f: f.get("type") == "response" and f.get("id") == "req-42")
    assert response["success"] is True
    assert response["command"] == "prompt"

    end = await client.recv_until(lambda f: f.get("type") == "agent_end")
    assert end["sessionId"] == "main"

    # Re-read the stream we already consumed is not possible, so assert on the
    # events that must arrive before agent_end using a second turn.
    await client.send({"type": "prompt", "message": "again"})
    seen: list[dict] = []
    while True:
        frame = await client.recv_until(lambda f: f.get("type") not in ("host_hello",))
        seen.append(frame)
        if frame.get("type") == "agent_end":
            break
    types = [f["type"] for f in seen]
    assert types[0] == "agent_start"
    assert "message_update" in types
    deltas = "".join(
        f["assistantMessageEvent"]["delta"] for f in seen if f.get("type") == "message_update"
    )
    assert "again" in deltas
    cursors = [f["hostSeq"] for f in seen if "hostSeq" in f]
    assert cursors == sorted(cursors)
    await client.close()


async def test_late_joiner_receives_history_and_a_boundary(make_server, config, registry, device_key):
    srv, first = await _connected(make_server, config, registry, device_key)
    await first.subscribe("main")
    await first.send({"type": "prompt", "message": "remember the milk"})
    end = await first.recv_until(lambda f: f.get("type") == "agent_end")
    first_boundary = end["hostSeq"]

    key, public_b64 = device_key
    srv.registry.add("phone-2", "Second", public_b64, kind="mobile")
    second = WssClient(srv.port, "phone-2", key)
    await second.connect()
    hello = await second.subscribe("main")
    assert [m["role"] for m in hello["messages"]] == ["user", "assistant"]
    assert hello["messages"][0]["content"] == "remember the milk"
    # The boundary is at or past every frame the first client already saw, so
    # replaying clients discard duplicates rather than rendering twice.
    assert hello["boundary"] >= first_boundary
    await first.close()
    await second.close()


async def test_two_clients_both_see_a_turn(make_server, config, registry, device_key):
    srv, one = await _connected(make_server, config, registry, device_key)
    await one.subscribe("main")
    key, public_b64 = device_key
    srv.registry.add("phone-2", "Second", public_b64, kind="mobile")
    two = WssClient(srv.port, "phone-2", key)
    await two.connect()
    await two.subscribe("main")

    await one.send({"type": "prompt", "message": "broadcast"})
    end_one = await one.recv_until(lambda f: f.get("type") == "agent_end")
    end_two = await two.recv_until(lambda f: f.get("type") == "agent_end")
    assert end_one["hostSeq"] == end_two["hostSeq"]
    await one.close()
    await two.close()


async def test_unicode_line_separators_survive_the_whole_path(make_server, config, registry, device_key):
    srv, client = await _connected(make_server, config, registry, device_key)
    await client.subscribe("main")
    tricky = "before\u2028middle\u2029after"
    await client.send({"type": "prompt", "message": tricky})
    await client.recv_until(lambda f: f.get("type") == "agent_end")

    # A resync re-reads history through JSONL, proving the string round-tripped
    # intact, including the separators a Node readline would have split on.
    await client.send({"type": "resync"})
    hello = await client.recv(expect="host_hello")
    assert hello["messages"][0]["content"] == tricky
    await client.close()


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------


async def test_overflow_triggers_an_automatic_resync(make_server, config, registry, device_key):
    config.client_queue_frames = 2
    srv, client = await _connected(make_server, config, registry, device_key)
    await client.subscribe("main")

    bridge = await srv.sessions.get_existing("main")
    assert bridge is not None
    sink = bridge.sinks[0]
    # Force the exact condition the real world produces when a phone sleeps
    # mid-stream: more frames than the client's queue can hold, with nobody
    # draining (the pump is racing, so fill far past the cap).
    for seq in range(50):
        sink.offer({"type": "message_update", "hostSeq": 10_000 + seq, "sessionId": "main"})

    hello = await client.recv(expect="host_hello", timeout=20)
    assert hello["resync"] is True
    assert sink.lagged is False
    assert hello["boundary"] >= bridge.host_seq
    await client.close()


# ---------------------------------------------------------------------------
# Host commands
# ---------------------------------------------------------------------------


async def test_session_status_reports_the_supervised_child(make_server, config, registry, device_key):
    srv, client = await _connected(make_server, config, registry, device_key)
    await client.subscribe("main")
    await client.send({"type": "session_status", "id": "s1"})
    status = await client.recv_until(lambda f: f.get("type") == "host_session_status")
    assert status["alive"] is True
    assert status["sessionId"] == "main"
    assert isinstance(status["pid"], int)
    await client.close()


async def test_list_sessions_includes_the_running_session(make_server, config, registry, device_key):
    srv, client = await _connected(make_server, config, registry, device_key)
    await client.subscribe("main")
    await client.send({"type": "list_sessions", "id": "l1"})
    listing = await client.recv_until(lambda f: f.get("type") == "host_sessions")
    assert any(s["sessionId"] == "main" and s["alive"] for s in listing["sessions"])
    await client.close()


async def test_new_session_switches_the_connection(make_server, config, registry, device_key):
    srv, client = await _connected(make_server, config, registry, device_key)
    await client.subscribe("main")
    await client.send({"type": "new_session", "sessionId": "second"})
    hello = await client.recv(expect="host_hello")
    assert hello["sessionId"] == "second"
    assert hello["messages"] == []
    assert {s["sessionId"] for s in srv.list_sessions_payload()} >= {"main", "second"}
    await client.close()


async def test_invalid_session_id_is_rejected(make_server, config, registry, device_key):
    srv, client = await _connected(make_server, config, registry, device_key)
    await client.send({"type": "subscribe", "sessionId": "../../etc"})
    error = await client.recv(expect="host_error")
    assert error["code"] == "bad_session_id"
    await client.close()


# ---------------------------------------------------------------------------
# The local model is declared, never inferred
# ---------------------------------------------------------------------------


async def test_hello_reports_the_manifest_model_and_licence(make_server, config, registry, device_key):
    """The client can show the model and its licence state before the first prompt."""
    config.model_manifest = REPO / "config" / "models.json"
    spec = load_manifest(config.model_manifest).active
    srv, client = await _connected(make_server, config, registry, device_key)
    hello = await client.subscribe("main")
    assert hello["localModel"] == spec.as_public_dict()
    assert hello["localModel"]["licence"]["status"] == spec.licence.status
    await client.close()
