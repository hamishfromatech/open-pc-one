"""The first-boot UI, driven over HTTP exactly as the kiosk browser drives it.

No mocks of our own code: a real server on an ephemeral port, a real manifest
file, real form posts, and the real install thread (with ``dry_run`` so nothing
touches the host).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

import pytest
from amaara_setup.license import key_hash  # noqa: E402
from amaara_setup.web import SetupApp, make_server
from helpers import issued as make_issued
from helpers import signing_keys as make_keys


@pytest.fixture
def ui(tmp_path, issued, signing_keys):
    manifest, keys = issued
    private_pem, public_pem = signing_keys
    manifest_path = tmp_path / "license.json"
    manifest_path.write_text(json.dumps(manifest.to_json()))
    public_path = tmp_path / "license.pub"
    public_path.write_bytes(public_pem)

    app = SetupApp(
        tmp_path / "state.json",
        manifest_path,
        public_path,
        dry_run=True,
        allow_poweroff=False,
    )
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, app, keys
    server.shutdown()
    server.server_close()


class Client:
    """Tiny HTTP client that does not follow redirects, so they can be asserted."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()), NoRedirect()
        )

    def get(self, path: str) -> tuple[int, str]:
        return self._do(urllib.request.Request(self.base + path))

    def post(self, path: str, data: dict[str, list[str]]) -> tuple[int, str, dict]:
        body = urllib.parse.urlencode(data, doseq=True).encode()
        request = urllib.request.Request(self.base + path, data=body, method="POST")
        return self._do(request)

    def _do(self, request) -> tuple:
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, response.read().decode(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(), dict(exc.headers)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def test_the_welcome_screen_is_the_license_screen(ui):
    base, _, _ = ui
    status, body, _ = Client(base).get("/")
    assert status == 200
    assert "Welcome to" in body and "AmaaraOS" in body
    assert 'name="key"' in body
    assert "power off and ship it" in body.lower()


def test_the_oem_check_records_and_consumes_nothing(ui):
    base, app, _ = ui
    status, body, _ = Client(base).post("/poweroff", {})
    assert status == 200
    assert "ready to ship" in body.lower()
    state = app.state
    assert state.factory_checks == 1
    assert state.phase == "welcome"
    assert state.license_id == ""


def test_a_wrong_key_is_refused_with_a_reason(ui):
    base, app, _ = ui
    status, body, _ = Client(base).post("/license", {"key": ["AMAARA-00000-00000-00000-00000"]})
    assert status == 400
    assert "not issued" in body
    assert app.state.phase == "welcome"


def test_the_whole_flow_reaches_a_working_box(ui):
    base, app, keys = ui
    client = Client(base)

    status, _, headers = client.post("/license", {"key": [keys[0]]})
    assert status == 303 and headers["Location"] == "/software"
    assert app.state.phase == "licensed"

    status, body, _ = client.get("/software")
    assert status == 200 and "Choose what this" in body

    status, _, headers = client.post("/software", {"profiles": ["local-ai", "dev-tools"]})
    assert status == 303 and headers["Location"] == "/installing"

    for _ in range(100):
        status, body, _ = client.get("/api/progress")
        payload = json.loads(body)
        if payload["complete"]:
            break
        time.sleep(0.05)
    assert payload["complete"], "the install thread never finished"

    result = payload["result"]
    assert result["dryRun"] is True
    targets = [step["target"] for step in result["steps"]]
    assert "openpc-engine.service" in targets, "the agent runtime is not optional"
    assert any("git" in target for target in targets)

    # Units-only profiles always land; a package profile depends on a network
    # this test does not promise, so the honest assertions are: the agent runtime
    # is there, and every failure names its reason.
    assert {"agent", "local-ai"} <= set(app.state.installed)
    for step in result["steps"]:
        if not step["ok"]:
            assert step["detail"], f"{step['target']} failed with no explanation"

    status, body, _ = client.get("/")
    assert "This computer is" in body and "yours" in body
    assert app.state.phase == "complete"


def test_setup_cannot_be_skipped_by_posting_to_software_first(ui):
    base, app, _ = ui
    status, _, headers = Client(base).get("/software")
    assert status == 303, "an unlicensed box must be sent back to the license screen"
    assert headers["Location"] == "/"


def test_state_endpoint_is_json_for_support(ui):
    base, _, _ = ui
    status, body, _ = Client(base).get("/state")
    payload = json.loads(body)
    assert status == 200
    assert payload["phase"] == "welcome"
    assert payload["deviceId"].startswith("OPC-")


def test_the_key_is_never_written_to_the_state_file(ui, tmp_path):
    base, app, keys = ui
    Client(base).post("/license", {"key": [keys[0]]})
    raw = (tmp_path / "state.json").read_text()
    assert keys[0] not in raw
    assert key_hash(keys[0]) not in raw, "not even the hash is needed after verification"
    assert "BATCH-TEST" in raw


@pytest.fixture
def signing_keys():
    return make_keys()


@pytest.fixture
def issued(signing_keys):
    return make_issued(signing_keys)
