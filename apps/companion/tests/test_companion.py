"""The companion's protocol and page, without a box and without a browser.

The frames are the contract with the appliance, so the tests are about frames: a
prompt names its field ``message`` (``prompt`` is silently ignored by the RPC
child), the pair frame carries the public key, and the session must fold a stream
of deltas into one assistant message instead of a message per token.
"""

from __future__ import annotations

import json

import companion_testkit  # noqa: F401  (puts src/ on sys.path)
import pytest
from amaara_companion.client import AUTH_PREFIX, Companion, DeviceIdentity
from amaara_companion.ui import Session, make_server


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class FakeCompanion:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.aborted = 0

    def prompt(self, text: str) -> None:
        self.prompts.append(text)

    def abort(self) -> None:
        self.aborted += 1


def test_identity_is_ed25519_and_signs_the_challenge():
    identity = DeviceIdentity()
    signature = identity.sign(f"{AUTH_PREFIX}abc".encode())
    assert isinstance(signature, str) and len(signature) > 40
    assert identity.public_key_b64 and len(identity.public_key_b64) == 44


def test_prompt_uses_the_message_field_not_prompt():
    companion = Companion("127.0.0.1:1")
    companion.ws = FakeSocket()
    companion.prompt("hello")
    frame = companion.ws.sent[-1]
    assert frame == {"type": "prompt", "id": "p1", "message": "hello"}
    assert "prompt" not in frame, "the RPC child ignores a `prompt` key"


def test_stats_and_control_frames():
    companion = Companion("127.0.0.1:1")
    companion.ws = FakeSocket()
    companion.stats()
    companion.session_status()
    companion.list_sessions()
    kinds = [frame["type"] for frame in companion.ws.sent]
    assert kinds == ["host_stats", "session_status", "list_sessions"]


def test_session_folds_deltas_into_one_message():
    session = Session(FakeCompanion())
    session.on_frame({"type": "message_start"})
    for word in ("Paired", " and", " ready", "."):
        session.on_frame(
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": word}}
        )
    session.on_frame({"type": "message_end"})
    snapshot = session.snapshot()
    assert [m["text"] for m in snapshot["messages"]] == ["Paired and ready."]
    assert snapshot["streaming"] is False


def test_session_keeps_stats_and_says_how_old_they_are():
    session = Session(FakeCompanion())
    session.on_frame({"type": "host_stats", "stats": {"cpu": {"percent": 12.5}}})
    snapshot = session.snapshot()
    assert snapshot["stats"]["cpu"]["percent"] == 12.5
    assert snapshot["statsAgeSeconds"] is not None


def test_an_engine_error_is_shown_not_swallowed():
    session = Session(FakeCompanion())
    session.on_frame({"type": "host_error", "code": "engine_down", "message": "child died"})
    assert "child died" in session.snapshot()["error"]


def test_prompts_are_forwarded_and_empty_ones_dropped():
    fake = FakeCompanion()
    session = Session(fake)
    session.send_prompt("   ")
    session.send_prompt("real question")
    assert fake.prompts == ["real question"]
    assert session.snapshot()["messages"][0] == {"role": "user", "text": "real question"}


@pytest.fixture
def page():
    session = Session(FakeCompanion())
    server = make_server(session, "127.0.0.1", 0)
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", session
    server.shutdown()
    server.server_close()


def test_the_page_loads_and_carries_the_brand(page):
    import urllib.request

    base, _ = page
    with urllib.request.urlopen(f"{base}/") as response:
        body = response.read().decode()
    assert response.status == 200
    for token in ("AmaaraOS", "#D72E35", "Ask the box", "Resources"):
        assert token in body


def test_state_endpoint_is_json(page):
    import urllib.request

    base, session = page
    session.on_frame({"type": "host_stats", "stats": {"memory": {"percent": 50.0}}})
    with urllib.request.urlopen(f"{base}/api/state") as response:
        payload = json.loads(response.read())
    assert payload["state"] == "connecting"
    assert payload["stats"]["memory"]["percent"] == 50.0


def test_prompt_endpoint_forwards_to_the_box(page):
    import urllib.request

    base, session = page
    request = urllib.request.Request(
        f"{base}/api/prompt",
        data=json.dumps({"text": "what is on the desktop?"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        assert json.loads(response.read())["ok"] is True
    assert session.companion.prompts == ["what is on the desktop?"]


def test_abort_endpoint_reaches_the_client(page):
    import urllib.request

    base, session = page
    request = urllib.request.Request(f"{base}/api/abort", data=b"", method="POST")
    with urllib.request.urlopen(request) as response:
        assert json.loads(response.read())["ok"] is True
    assert session.companion.aborted == 1


def test_the_pin_falls_back_to_what_the_box_reports():
    """A person must be able to compare a pin even where the transport withholds it."""
    session = Session(FakeCompanion())
    session.appliance = {"certFingerprint": "ab" * 32}
    assert session.snapshot()["pin"] == "ab" * 32
