"""The model layer is configuration: parsing, validation, and the client shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpc_engine.config import EngineConfig
from openpc_engine.models import (
    ManifestError,
    ModelLicence,
    ModelSpec,
    load_manifest,
    parse_manifest,
)
from openpc_engine.ws_server import EngineServer

REPO = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = REPO / "config" / "models.json"


def manifest_payload(**overrides) -> dict:
    model = {
        "displayName": "Test Model",
        "repoId": "example/test-model-GGUF",
        "quant": "Q4_K_M",
        "file": "test-model.gguf",
        "contextLength": 8192,
        "licence": {"id": "Apache-2.0", "status": "verified", "url": "https://example.test"},
        "llamaCpp": {
            "architecture": "test",
            "artifact": "llama.cpp-test.tar.gz",
            "args": ["--ctx-size", "8192", "--flash-attn"],
        },
    }
    model.update(overrides.pop("model", {}))
    payload = {"schema": "openpc.models/1", "active": "test", "models": {"test": model}}
    payload.update(overrides)
    return payload


def write_manifest(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "models.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The shipped manifest
# ---------------------------------------------------------------------------


def test_default_manifest_declares_the_256k_model():
    manifest = load_manifest(DEFAULT_MANIFEST)
    spec = manifest.active
    assert spec.repo_id == "LiquidAI/LFM3-1.7B-GGUF"
    assert spec.quant == "Q4_K_M"
    assert spec.context_length == 262144
    assert spec.llama_architecture == "lfm3"
    assert spec.llama_artifact == "llama.cpp-lfm3.tar.gz"
    # The retail gate exists because this is not verified yet. If the terms land,
    # flip it deliberately - this assertion is the reminder.
    assert spec.licence.status == "unverified"


def test_llama_argv_is_model_first_then_the_manifest_flags():
    manifest = load_manifest(DEFAULT_MANIFEST)
    argv = manifest.active.llama_argv()
    assert argv[:2] == ["--model", "/var/lib/openpc/models/LFM3-1.7B-Q4_K_M.gguf"]
    assert argv[2:] == list(manifest.active.llama_args)
    assert argv.index("--ctx-size") + 1 == argv.index("262144")


def test_public_dict_is_the_client_shape_not_the_launcher_arguments():
    spec = load_manifest(DEFAULT_MANIFEST).active
    public = spec.as_public_dict()
    assert public["id"] == spec.key
    assert public["repoId"] == spec.repo_id
    assert public["contextLength"] == spec.context_length
    assert public["licence"]["status"] == "unverified"
    # Flags are host configuration; clients should not learn how to spawn it.
    assert "llamaCpp" not in public
    assert "file" not in public


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_active_model_must_be_declared(tmp_path):
    path = write_manifest(tmp_path, manifest_payload(active="missing"))
    with pytest.raises(ManifestError, match="not declared"):
        load_manifest(path)


def test_licence_status_must_be_known_so_the_gate_cannot_be_typoed(tmp_path):
    payload = manifest_payload()
    payload["models"]["test"]["licence"]["status"] = "probably-fine"
    path = write_manifest(tmp_path, payload)
    with pytest.raises(ManifestError, match="licence.status"):
        load_manifest(path)


def test_model_file_cannot_escape_the_model_directory(tmp_path):
    path = write_manifest(tmp_path, manifest_payload(model={"file": "../etc/shadow"}))
    with pytest.raises(ManifestError, match="bare filename"):
        load_manifest(path)


def test_context_length_must_be_positive(tmp_path):
    path = write_manifest(tmp_path, manifest_payload(model={"contextLength": 0}))
    with pytest.raises(ManifestError, match="positive integer"):
        load_manifest(path)


def test_missing_manifest_is_a_clear_error(tmp_path):
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path / "nope.json")


def test_unknown_schema_is_refused(tmp_path):
    path = write_manifest(tmp_path, manifest_payload(schema="openpc.models/999"))
    with pytest.raises(ManifestError, match="unsupported schema"):
        load_manifest(path)


def test_parse_manifest_does_not_require_a_file():
    parsed = parse_manifest(manifest_payload(), path=Path("/virtual/models.json"))
    assert isinstance(parsed.active, ModelSpec)
    assert isinstance(parsed.active.licence, ModelLicence)


# ---------------------------------------------------------------------------
# The engine reads it, and never dies because of it
# ---------------------------------------------------------------------------


def test_engine_loads_the_manifest_for_host_hello(config, tmp_path):
    config.model_manifest = write_manifest(tmp_path, manifest_payload())
    server = EngineServer(config)
    assert server.local_model is not None
    assert server.local_model.as_public_dict()["id"] == "test"


def test_a_broken_manifest_does_not_stop_the_edge_from_serving(config, tmp_path):
    broken = tmp_path / "models.json"
    broken.write_text("{not json", encoding="utf-8")
    config.model_manifest = broken
    server = EngineServer(config)
    assert server.local_model is None
