"""Downloading and the refusal that follows it.

The hash check is the point: a feed can be compromised after signing (the bytes at
the URL change), and the client must catch that before anything is written to a
slot. These tests use file:// URLs so the whole path is exercised with no network.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import update_testkit as helpers  # noqa: F401  (puts src/ on sys.path)
from amaara_update import cli
from amaara_update.manifest import Image, Manifest, ManifestError, sign

PAYLOAD = b"x" * 4096


@pytest.fixture
def feed(tmp_path):
    image = tmp_path / "image.raw"
    image.write_bytes(PAYLOAD)
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    return tmp_path, image, digest


def _manifest(image_url: str, digest: str, size: int = len(PAYLOAD)) -> Manifest:
    return Manifest(
        version="1.1.0",
        channel="pilot",
        released="2026-11-20T00:00:00Z",
        image=Image(url=image_url, sha256=digest, size=size),
    )


def test_download_verifies_and_keeps_the_file(feed, tmp_path):
    _, image, digest = feed
    manifest = _manifest(image.as_uri(), digest)
    staged = cli.download(manifest, tmp_path / "staging")
    assert staged.read_bytes() == PAYLOAD
    assert staged.name == "amaaraos-1.1.0.raw"


def test_a_changed_download_is_deleted_not_used(feed, tmp_path):
    _, image, digest = feed
    manifest = _manifest(image.as_uri(), "0" * 64)  # signed hash does not match bytes
    with pytest.raises(ManifestError, match="signed hash"):
        cli.download(manifest, tmp_path / "staging")
    assert not list((tmp_path / "staging").glob("*.raw")), "a bad download must not linger"


def test_a_short_download_is_refused(feed, tmp_path):
    _, image, digest = feed
    manifest = _manifest(image.as_uri(), digest, size=len(PAYLOAD) + 10)
    with pytest.raises(ManifestError, match="manifest says"):
        cli.download(manifest, tmp_path / "staging")


def test_no_public_key_means_no_feed(tmp_path):
    config = cli.Config(feed_url="file:///nonexistent")
    with pytest.raises(ManifestError, match="public key"):
        cli.resolve_manifest(config, tmp_path / "missing.pub")


def test_no_feed_url_is_refused_before_any_request(keypair_path, tmp_path):
    config = cli.Config(feed_url="")
    with pytest.raises(ManifestError, match="FEED_URL"):
        cli.resolve_manifest(config, keypair_path)


def test_config_parses_the_appliance_file(tmp_path):
    path = tmp_path / "update.conf"
    path.write_text(
        "# comment\nFEED_URL=https://feed.example/amaaraos\nCHANNEL=retail\nAUTO_APPLY=yes\n"
    )
    config = cli.Config.load(path)
    assert config.feed_url == "https://feed.example/amaaraos"
    assert config.channel == "retail"
    assert config.auto_apply is True


def test_a_missing_config_is_not_a_crash(tmp_path):
    config = cli.Config.load(tmp_path / "nope.conf")
    assert config.feed_url == ""
    assert config.channel == "pilot"


def test_current_version_reads_our_os_release(monkeypatch, tmp_path):
    release = tmp_path / "os-release"
    release.write_text('NAME="AmaaraOS"\nID=amaaraos\nVERSION="1.2.3"\n')
    monkeypatch.setattr(cli, "OS_RELEASE", release)
    assert cli.current_version() == "1.2.3"


def test_a_foreign_root_is_refused_not_compared(monkeypatch, tmp_path):
    # Debian's VERSION="12 (bookworm)" compares as 12 and would beat every
    # AmaaraOS release, leaving the box permanently 'up to date'.
    release = tmp_path / "os-release"
    release.write_text('ID=debian\nVERSION="12 (bookworm)"\n')
    monkeypatch.setattr(cli, "OS_RELEASE", release)
    assert cli.current_version() == ""


def test_check_records_the_outcome(tmp_path, keypair_path, monkeypatch):
    """A check must leave a trace: the Settings pane shows this file."""
    manifest = Manifest(
        version="9.9.9",
        channel="pilot",
        released="2026-11-20T00:00:00Z",
        image=Image(url="file:///nonexistent.raw", sha256="0" * 64, size=1),
    )
    monkeypatch.setattr(cli, "resolve_manifest", lambda config, key: manifest)
    monkeypatch.setattr(cli, "current_version", lambda: "1.0.0")
    state = tmp_path / "state"
    assert cli.cmd_check(cli.Config(feed_url="https://feed.example"), keypair_path, state) == 0
    recorded = json.loads((state / "last-check.json").read_text())
    assert recorded["status"] == "update-available"
    assert recorded["version"] == "9.9.9"


@pytest.fixture
def keypair_path(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    path = tmp_path / "update.pub"
    path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return path


def test_sign_round_trips_through_the_written_file(tmp_path, keypair_path):
    """`sign` is our side of the desk; the client must accept its output."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private_pem = tmp_path / "update.private.pem"
    private_pem.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public = tmp_path / "signed.pub"
    public.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    document = sign(_manifest("file:///x.raw", "b" * 64), private_pem.read_bytes())
    signed = tmp_path / "manifest.json"
    signed.write_text(json.dumps(document))
    assert cli.cmd_verify(signed, public) == 0
    # and the same document fails against a different key
    assert cli.cmd_verify(signed, keypair_path) == cli.EXIT_REFUSED
