"""The two wire contracts, and the provenance of the vendored REST one.

The REST surface is not re-invented here: `contract/openapi.json` is a verbatim
copy of the Open-PC dashboard spec that already generates the Tauri and Flutter
clients. The hash file is what stops it drifting silently.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CONTRACT = Path(__file__).resolve().parents[2] / "contract"
OPENAPI = CONTRACT / "openapi.json"
ASYNCAPI = CONTRACT / "engine.asyncapi.yaml"


def test_vendored_openapi_is_the_open_pc_agent_api():
    spec = json.loads(OPENAPI.read_text(encoding="utf-8"))
    assert spec["openapi"] == "3.1.0"
    assert spec["info"]["title"] == "Open-PC Agent API"
    assert len(spec["paths"]) >= 30
    assert len(spec["components"]["schemas"]) >= 15


def test_vendored_openapi_keeps_the_paths_the_clients_use():
    spec = json.loads(OPENAPI.read_text(encoding="utf-8"))
    for path in ("/health", "/capabilities", "/apps/launch", "/keyboard/type", "/screenshot"):
        assert path in spec["paths"], f"REST contract lost {path}"


def test_openapi_hash_records_what_is_actually_committed():
    expected = (CONTRACT / "openapi.sha256").read_text(encoding="utf-8").split()[0]
    actual = hashlib.sha256(OPENAPI.read_bytes()).hexdigest()
    assert actual == expected, (
        "contract/openapi.json was edited or replaced without updating openapi.sha256; "
        "re-copy it from the dashboard repo's dashboard/openapi.json and re-hash "
        "(see contract/README.md)"
    )


def test_asyncapi_is_the_wss_contract_and_carries_the_local_model():
    text = ASYNCAPI.read_text(encoding="utf-8")
    assert "asyncapi: 3.0.0" in text
    assert "pathname: /v1/rpc" in text
    assert "host_hello" in text
    assert "localModel:" in text
