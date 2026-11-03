"""Shared fixtures. The bridge is always exercised against ``fake_engine.py``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from openpc_engine.auth import DeviceRegistry, new_device_keypair  # noqa: E402
from openpc_engine.config import EngineConfig  # noqa: E402

FAKE_ENGINE = ROOT / "tests" / "fake_engine.py"


@pytest.fixture
def fake_engine_command() -> tuple[str, ...]:
    return (sys.executable, str(FAKE_ENGINE))


@pytest.fixture
def config(tmp_path, fake_engine_command) -> EngineConfig:
    cfg = EngineConfig(
        bind_host="127.0.0.1",
        bind_port=0,
        state_dir=tmp_path / "state",
        session_root=tmp_path / "state" / "sessions",
        devices_file=tmp_path / "state" / "devices.json",
        pairing_file=tmp_path / "state" / "pairing.json",
        tls_cert=tmp_path / "state" / "tls" / "engine.crt",
        tls_key=tmp_path / "state" / "tls" / "engine.key",
        engine_command=fake_engine_command,
        agent_cwd=tmp_path / "agent",
        engine_start_timeout=20.0,
        idle_timeout=0.25,
        idle_sweep_interval=0.05,
        replay_limit=50,
        client_queue_frames=64,
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def registry(config) -> DeviceRegistry:
    return DeviceRegistry(config.devices_file)


@pytest.fixture
def device_key():
    key, public_b64 = new_device_keypair()
    return key, public_b64


@pytest.fixture
def registered_device(registry, device_key):
    key, public_b64 = device_key
    record = registry.add("phone-1", "Pixel", public_b64, kind="mobile")
    return record, key
