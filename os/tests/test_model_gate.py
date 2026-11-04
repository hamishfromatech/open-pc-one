"""The licence gate and the manifest -> image rendering path.

These run the real shell entry points (``model-config``, ``pin-artifacts``) with
fixture manifests, because the requirement is about what the *build* refuses, not
about what a Python function returns.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

OS = Path(__file__).resolve().parents[1]
REPO = OS.parent
SCRIPTS = OS / "scripts"
MODEL_CONFIG = SCRIPTS / "model-config"
DEFAULT_MANIFEST = REPO / "config" / "models.json"


def manifest_payload(*, status: str = "unverified", artifact: str = "llama.cpp-lfm3.tar.gz") -> dict:
    return {
        "schema": "openpc.models/1",
        "active": "test-1.7b",
        "models": {
            "test-1.7b": {
                "displayName": "Test 1.7B",
                "repoId": "example/test-1.7b-GGUF",
                "quant": "Q4_K_M",
                "file": "test-1.7b.gguf",
                "contextLength": 262144,
                "licence": {"id": "Example Research Licence", "status": status},
                "llamaCpp": {
                    "architecture": "test",
                    "artifact": artifact,
                    "args": ["--ctx-size", "262144", "--flash-attn"],
                },
            }
        },
    }


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(manifest_payload()), encoding="utf-8")
    return path


def run_model_config(manifest: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MODEL_CONFIG), "--manifest", str(manifest), *args],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_retail_refuses_an_unverified_licence(manifest):
    result = run_model_config(manifest, "check", "--target", "retail")
    assert result.returncode != 0
    assert "REFUSING retail build" in result.stderr
    assert "Example Research Licence" in result.stderr
    assert "not be sold" not in result.stdout


def test_dev_and_pilot_proceed_with_a_warning(manifest):
    for target in ("dev", "pilot"):
        result = run_model_config(manifest, "check", "--target", target)
        assert result.returncode == 0, result.stderr
        assert "WARNING" in result.stderr
        assert target in result.stdout


def test_retail_accepts_a_verified_licence(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(manifest_payload(status="verified")), encoding="utf-8")
    result = run_model_config(path, "check", "--target", "retail")
    assert result.returncode == 0, result.stderr
    assert "licence=verified" in result.stdout


@pytest.mark.parametrize("subcommand", ["check", "render", "artifacts", "show"])
def test_manifest_is_accepted_after_the_subcommand(manifest, tmp_path, subcommand):
    """build-image and mkosi.postinst pass --manifest after the subcommand.

    argparse only accepts an option on the parser that declared it, so a single
    top-level declaration silently breaks every caller that uses the documented
    form. This pins both positions.
    """
    args = [str(MODEL_CONFIG), subcommand, "--manifest", str(manifest)]
    if subcommand == "check":
        args += ["--target", "pilot"]
    elif subcommand == "render":
        args += ["--out", str(tmp_path / "out")]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "unrecognized arguments" not in result.stderr


def test_the_shipped_manifest_is_blocked_for_retail_today():
    """The LFM3 licence is still `other`; a retail build must fail closed."""
    result = run_model_config(DEFAULT_MANIFEST, "check", "--target", "retail")
    assert result.returncode == 2
    assert "LFM Open License" in result.stderr


def test_a_malformed_manifest_fails_every_target(manifest):
    manifest.write_text("{", encoding="utf-8")
    for target in ("dev", "pilot", "retail"):
        result = run_model_config(manifest, "check", "--target", target)
        assert result.returncode == 2
        assert "invalid JSON" in result.stderr


# ---------------------------------------------------------------------------
# Rendering: the model reaches llama-server through data, not unit files
# ---------------------------------------------------------------------------


def test_render_writes_argv_model_summary_and_the_manifest(manifest, tmp_path):
    out = tmp_path / "etc-openpc"
    result = run_model_config(manifest, "render", "--out", str(out))
    assert result.returncode == 0, result.stderr

    args = (out / "llama.args").read_text().splitlines()
    assert args[:2] == ["--model", "/var/lib/openpc/models/test-1.7b.gguf"]
    assert args[2:] == ["--ctx-size", "262144", "--flash-attn"]

    public = json.loads((out / "model.json").read_text())
    assert public["id"] == "test-1.7b"
    assert public["licence"]["status"] == "unverified"
    assert "args" not in json.dumps(public)

    # The image carries the manifest it was built from.
    assert json.loads((out / "models.json").read_text())["active"] == "test-1.7b"


def test_render_refuses_a_filename_that_escapes_the_model_directory(tmp_path):
    payload = manifest_payload()
    payload["models"]["test-1.7b"]["file"] = "../../etc/passwd"
    path = tmp_path / "models.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = run_model_config(path, "render", "--out", str(tmp_path / "out"))
    assert result.returncode == 2
    assert "bare filename" in result.stderr


def test_artifacts_prints_the_manifest_llama_tarball(manifest):
    result = run_model_config(manifest, "artifacts")
    assert result.returncode == 0
    assert result.stdout.strip() == "llama.cpp-lfm3.tar.gz"


# ---------------------------------------------------------------------------
# The build entry points
# ---------------------------------------------------------------------------


def test_build_image_runs_the_gate_before_anything_expensive():
    text = (SCRIPTS / "build-image").read_text()
    gate = text.index('model-config" check')
    wheels = text.index("make wheels")
    mkosi = text.index("exec mkosi")
    assert gate < wheels < mkosi
    assert "$HERE/build-target" in text
    # --target is parsed, not positional, and defaults to a non-retail target.
    assert "--target" in text
    assert 'TARGET="${OPENPC_BUILD_TARGET:-dev}"' in text


def test_postinst_reruns_the_gate_and_renders_from_the_manifest():
    text = (OS / "image" / "mkosi.postinst").read_text()
    assert 'BUILD_TARGET="$(cat "$REPO/os/image/build-target"' in text
    assert 'model-config" check --target "$BUILD_TARGET"' in text
    assert 'model-config" render --out "$BUILDROOT/etc/openpc"' in text
    assert "llama.cpp-lfm3.tar.gz" not in text


def test_pin_artifacts_takes_the_llama_artifact_name_from_the_manifest(tmp_path):
    artefact_dir = tmp_path / "artifacts"
    artefact_dir.mkdir()
    for name in ("cua-driver", "a-coder-cli", "llama.cpp-generic.tar.gz"):
        (artefact_dir / name).write_bytes(name.encode() * 8)

    manifest = tmp_path / "models.json"
    manifest.write_text(json.dumps(manifest_payload(artifact="llama.cpp-generic.tar.gz")), encoding="utf-8")

    env = {**os.environ, "OPENPC_MODEL_MANIFEST": str(manifest)}
    result = subprocess.run(
        ["bash", str(SCRIPTS / "pin-artifacts"), str(artefact_dir)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    names = sorted(line.split("  ")[1] for line in (artefact_dir / "SHA256SUMS").read_text().splitlines())
    assert names == ["a-coder-cli", "cua-driver", "llama.cpp-generic.tar.gz"]


def test_no_unit_or_engine_source_names_a_model():
    """Swapping models must stay a manifest change, not a code change."""
    roots = [OS / "overlay", REPO / "services" / "engine" / "src"]
    offenders = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(errors="ignore")
            for needle in ("LFM3", "LiquidAI", "lfm3"):
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO)}: {needle}")
    assert offenders == []


def test_llama_unit_delegates_to_the_rendered_argv():
    unit = (OS / "overlay" / "etc" / "systemd" / "system" / "llama.service").read_text()
    assert "ExecStart=/opt/openpc/bin/openpc-llama" in unit
    assert "--model" not in unit
    assert "--ctx-size" not in unit


def test_launcher_keeps_one_argument_per_line(tmp_path):
    """An argument with a space must not be word-split by the launcher."""
    args = tmp_path / "llama.args"
    args.write_text("--model\n/var/lib/openpc/models/a b.gguf\n--ctx-size\n262144\n", encoding="utf-8")
    fake = tmp_path / "echo-argv"
    fake.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    fake.chmod(0o755)

    result = subprocess.run(
        ["bash", str(SCRIPTS / "openpc-llama")],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "OPENPC_LLAMA_ARGS": str(args), "OPENPC_LLAMA_BIN": str(fake)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["--model", "/var/lib/openpc/models/a b.gguf", "--ctx-size", "262144"]


def test_launcher_refuses_to_start_without_rendered_args(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPTS / "openpc-llama")],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "OPENPC_LLAMA_ARGS": str(tmp_path / "missing"), "OPENPC_LLAMA_BIN": "/bin/echo"},
    )
    assert result.returncode == 78
    assert "missing or empty" in result.stderr
