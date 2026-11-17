"""The state machine is the contract between the factory and the customer."""

from __future__ import annotations

import json
import stat

import helpers  # noqa: F401  (puts src/ on sys.path)
import pytest
from amaara_setup.state import SetupState, StateError


def test_a_fresh_device_starts_at_the_welcome_screen(tmp_path):
    state = SetupState.load(tmp_path / "state.json")
    assert state.phase == "welcome"
    assert state.device_id.startswith("OPC-")
    assert state.ready_for_customer


def test_the_oem_check_costs_the_customer_nothing(tmp_path):
    path = tmp_path / "state.json"
    state = SetupState.load(path)
    state.note_factory_check()
    state.save(path)

    reloaded = SetupState.load(path)
    assert reloaded.factory_checks == 1
    assert reloaded.phase == "welcome", "a checked unit still needs the customer's key"
    assert reloaded.license_id == ""


def test_legal_transitions():
    state = SetupState()
    state.move("licensed")
    state.move("installing")
    state.move("complete")
    assert state.complete


def test_illegal_transitions_raise():
    state = SetupState()
    with pytest.raises(StateError, match="cannot go from welcome to complete"):
        state.move("complete")
    with pytest.raises(StateError, match="unknown phase"):
        state.move("shipped")


def test_state_file_is_private_and_written_atomically(tmp_path):
    path = tmp_path / "state.json"
    state = SetupState.load(path)
    state.save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / "state.tmp").exists()
    json.loads(path.read_text())  # never a half-written file


def test_unknown_fields_are_ignored_so_old_images_still_boot(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"phase": "welcome", "device_id": "OPC-1", "futureField": 7}))
    state = SetupState.load(path)
    assert state.device_id == "OPC-1"
    assert "futureField" not in state.public()
