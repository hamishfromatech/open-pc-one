"""Amaara Engine is one file and one environment variable, and the key is neither.

The box must survive every state of this config: absent (offline box, local model
only), present without its key (misconfigured, must be visible as such), and broken
(must be reported, not silently ignored). No test here may ever print a key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpc_engine import providers


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(payload))
    return path


def test_missing_file_means_no_engine(tmp_path):
    state = providers.load(tmp_path / "absent.json")
    assert state.active == ""
    assert state.current() is None
    assert state.env() == {}
    assert state.public() == {"active": "", "provider": None, "count": 0}


def test_a_configured_engine_produces_child_environment(tmp_path):
    path = write(tmp_path, {
        "active": "amaara",
        "providers": {"amaara": {
            "kind": "openai",
            "baseUrl": "https://engine.example/v1",
            "model": "amaara/auto",
            "apiKeyEnv": "AMAARA_ENGINE_API_KEY",
        }},
    })
    environ = {"AMAARA_ENGINE_API_KEY": "sk-secret-value"}
    state = providers.load(path, environ)
    overlay = state.env(environ)

    assert overlay["OPENAI_BASE_URL"] == "https://engine.example/v1"
    assert overlay["OPENAI_API_KEY"] == "sk-secret-value"
    assert overlay["AMAARA_ENGINE_MODEL"] == "amaara/auto"
    # and it is never in the public shape
    public = json.dumps(state.public())
    assert "sk-secret-value" not in public
    assert state.public()["provider"]["hasKey"] is True


def test_a_missing_key_is_reported_not_invented(tmp_path):
    path = write(tmp_path, {
        "active": "amaara",
        "providers": {"amaara": {"baseUrl": "https://engine.example/v1",
                                 "apiKeyEnv": "AMAARA_ENGINE_API_KEY"}},
    })
    state = providers.load(path, {})
    overlay = state.env({})
    assert "OPENAI_API_KEY" not in overlay, "an empty key must not be exported"
    assert state.public()["provider"]["hasKey"] is False


def test_a_broken_file_is_an_error_not_a_silent_default(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    with pytest.raises(providers.ProviderError):
        providers.load(bad)


def test_active_must_exist(tmp_path):
    path = write(tmp_path, {"active": "ghost", "providers": {}})
    with pytest.raises(providers.ProviderError, match="not in the file"):
        providers.load(path)
