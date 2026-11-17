"""The CLI and the service must resolve the same paths from ``--state-dir``.

A half-mapped state root is invisible in unit tests (they build
``EngineConfig`` directly) and fatal in the field: ``pair-code`` writes the code
to one file and ``serve`` reads another, so no desktop client can ever pair.
"""

from __future__ import annotations

import json

from openpc_engine.auth import PairingCodeStore
from openpc_engine.cli import _config_from_args, build_parser, main


def test_state_dir_moves_every_state_path(tmp_path):
    args = build_parser().parse_args(["--state-dir", str(tmp_path), "pair-code"])
    config = _config_from_args(args)

    assert config.state_dir == tmp_path
    assert config.session_root == tmp_path / "sessions"
    assert config.devices_file == tmp_path / "devices.json"
    assert config.pairing_file == tmp_path / "pairing.json"
    assert config.tls_cert == tmp_path / "tls" / "engine.crt"
    assert config.tls_key == tmp_path / "tls" / "engine.key"


def test_pairing_code_minted_by_the_cli_reaches_a_running_service(tmp_path, capsys):
    """The operator mints a code in one process; ``serve`` is already up."""
    root = tmp_path / "state"
    serving = PairingCodeStore(ttl=600.0, path=root / "pairing.json")

    assert main(["--state-dir", str(root), "pair-code"]) == 0
    code = json.loads(capsys.readouterr().out)["code"]

    assert serving.redeem(code), "a code minted after start-up must be usable without a restart"
    assert not serving.redeem(code), "pairing codes stay single-use"
