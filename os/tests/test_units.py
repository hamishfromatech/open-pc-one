"""The appliance image is configuration, so it can be tested like code.

These tests encode requirements that are easy to break silently: boot order, the
hardening directives on each unit, journald caps, the firewall default, and the
A/B rollback mechanism.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

OS = Path(__file__).resolve().parents[1]
UNITS = OS / "overlay" / "etc" / "systemd" / "system"
SCRIPTS = OS / "scripts"


def parse_unit(path: Path) -> dict[str, list[str]]:
    """Parse a systemd unit into ``{key: [values]}`` without losing duplicates.

    ``configparser`` collapses repeated keys, and systemd merges them; boot order
    assertions need the full list.
    """
    sections: dict[str, dict[str, list[str]]] = {}
    current = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            sections.setdefault(current, {}).setdefault(key.strip(), []).append(value.strip())
    return {f"{section}.{key}": values for section, keys in sections.items() for key, values in keys.items()}


def values(path: Path, key: str) -> list[str]:
    return parse_unit(path).get(key, [])


def dependencies(path: Path, key: str) -> set[str]:
    return {item for value in values(path, key) for item in value.split()}


def directive(path: Path, section_key: str) -> str | None:
    entries = values(path, section_key)
    return entries[-1] if entries else None


# ---------------------------------------------------------------------------
# Boot order (brief: network-online -> session -> llama -> speech -> cua -> engine)
# ---------------------------------------------------------------------------


def test_every_required_unit_exists():
    for name in (
        "openpc-session.service",
        "llama.service",
        "openpc-speech.service",
        "cua-driver.service",
        "openpc-engine.service",
        "openpc-led.service",
        "openpc-provision.service",
        "openpc.target",
    ):
        assert (UNITS / name).exists(), f"missing unit {name}"


def test_session_waits_for_the_network():
    session = UNITS / "openpc-session.service"
    assert "network-online.target" in dependencies(session, "Unit.After")
    assert "network-online.target" in dependencies(session, "Unit.Wants")


def test_llama_starts_after_the_session_and_before_the_engine():
    llama = UNITS / "llama.service"
    assert "openpc-session.service" in dependencies(llama, "Unit.After")
    # The engine must never ask get_available_models before llama is up.
    assert "openpc-engine.service" in dependencies(llama, "Unit.Before")


def test_speech_follows_llama_and_precedes_cua_driver():
    speech = UNITS / "openpc-speech.service"
    assert "llama.service" in dependencies(speech, "Unit.After")
    assert "cua-driver.service" in dependencies(speech, "Unit.Before")


def test_cua_driver_precedes_the_engine():
    cua = UNITS / "cua-driver.service"
    assert "openpc-engine.service" in dependencies(cua, "Unit.Before")
    assert "openpc-session.service" in dependencies(cua, "Unit.After")


def test_engine_ordering_is_the_documented_chain():
    engine = UNITS / "openpc-engine.service"
    after = dependencies(engine, "Unit.After")
    for required in (
        "openpc-session.service",
        "llama.service",
        "openpc-speech.service",
        "cua-driver.service",
    ):
        assert required in after, f"engine is not ordered after {required}"
    assert "llama.service" in dependencies(engine, "Unit.Wants")


def test_engine_does_not_block_on_provisioning():
    """A phone that never arrives must not keep the agent offline."""
    engine = UNITS / "openpc-engine.service"
    assert "openpc-provision.service" not in dependencies(engine, "Unit.After")
    assert "openpc-provision.service" not in dependencies(engine, "Unit.Requires")


def test_provisioning_precedes_network_online_but_gates_nothing():
    provision = UNITS / "openpc-provision.service"
    assert "network-online.target" in dependencies(provision, "Unit.Before")
    assert not dependencies(provision, "Unit.Requires")


def test_led_starts_early():
    led = UNITS / "openpc-led.service"
    assert "openpc-session.service" in dependencies(led, "Unit.Before")


def test_target_pulls_in_the_whole_appliance():
    target = UNITS / "openpc.target"
    assert "openpc-led.service" in dependencies(target, "Unit.Requires")
    for unit in ("openpc-session.service", "llama.service", "openpc-engine.service"):
        assert unit in dependencies(target, "Unit.Wants")
    # default.target points at openpc.target, so also WantedBy=multi-user would
    # be an ordering cycle systemd has to break.
    assert not dependencies(target, "Install.WantedBy")
    assert "multi-user.target" in dependencies(target, "Unit.Wants")


# ---------------------------------------------------------------------------
# Hardening
# ---------------------------------------------------------------------------


UNPRIVILEGED_UNITS = [
    "openpc-session.service",
    "llama.service",
    "cua-driver.service",
    "openpc-engine.service",
    "openpc-provision.service",
]


@pytest.mark.parametrize("unit", UNPRIVILEGED_UNITS)
def test_units_are_sandboxed(unit):
    path = UNITS / unit
    assert directive(path, "Service.NoNewPrivileges") == "yes"
    assert directive(path, "Service.ProtectSystem") == "strict"
    assert directive(path, "Service.PrivateTmp") == "yes"


def test_engine_uses_systemd_owned_state_and_runtime_dirs():
    path = UNITS / "openpc-engine.service"
    assert directive(path, "Service.StateDirectory") == "openpc"
    assert directive(path, "Service.RuntimeDirectory") == "openpc"


def test_local_backends_are_loopback_only_and_the_engine_is_not():
    # llama/speech/cua-driver bind 127.0.0.1, so the socket filter is a second
    # lock. The engine is different: paired phones/desktop connect to 0.0.0.0:8095,
    # so its LAN exposure is enforced by nftables, not IPAddressAllow (which
    # would also reject the bind).
    llama = UNITS / "llama.service"
    assert "localhost" in values(llama, "Service.IPAddressAllow")
    cua = UNITS / "cua-driver.service"
    assert "localhost" in values(cua, "Service.IPAddressAllow")
    engine = UNITS / "openpc-engine.service"
    assert not values(engine, "Service.IPAddressAllow")


def test_journald_has_a_hard_budget():
    path = OS / "overlay" / "etc" / "systemd" / "journald.conf.d" / "openpc.conf"
    text = path.read_text()
    assert "SystemMaxUse=200M" in text
    assert "RateLimitBurst=" in text
    assert "MaxRetentionSec=" in text


def test_sysctl_disables_forwarding_and_hides_kernel_details():
    text = (OS / "overlay" / "etc" / "sysctl.d" / "90-openpc-hardening.conf").read_text()
    assert "net.ipv4.ip_forward = 0" in text
    assert "kernel.kptr_restrict = 2" in text
    assert "kernel.dmesg_restrict = 1" in text
    assert "net.ipv4.tcp_syncookies = 1" in text


def test_firewall_defaults_to_drop_inbound():
    text = (OS / "overlay" / "etc" / "nftables.conf").read_text()
    assert re.search(r"chain input \{.*?policy drop", text, re.S)
    # Both endpoints (engine :8095, agent server :8090) are reachable from the
    # LAN, never from 0.0.0.0/0.
    assert "tcp dport { 8090, 8095 } accept" in text
    assert "0.0.0.0/0" not in text


def test_chrome_profile_lives_on_tmpfs_and_precedes_the_session():
    mount = UNITS / "run-openpc-chrome.mount"
    assert mount.exists()
    data = parse_unit(mount)
    assert data["Mount.What"] == ["tmpfs"]
    assert "openpc-session.service" in dependencies(mount, "Unit.Before")


def test_hardware_watchdog_and_timeouts_are_configured():
    postinst = (OS / "image" / "mkosi.postinst").read_text()
    assert "RuntimeWatchdogSec=30s" in postinst
    assert "DefaultTimeoutStartSec=45s" in postinst


# ---------------------------------------------------------------------------
# A/B OTA
# ---------------------------------------------------------------------------


def test_ota_writes_the_inactive_slot_only():
    text = (SCRIPTS / "ota-update").read_text()
    assert "refusing to overwrite the running root" in text
    assert "openpc.slot=" in text
    # Three tries, then systemd-boot removes the entry on its own.
    assert "BOOT_COUNT=3" in text
    assert "set-oneshot" in text


def test_repart_defines_both_slots_and_a_separate_var():
    repart = OS / "image" / "mkosi.repart"
    labels = {p.name: p.read_text() for p in repart.glob("*.conf")}
    assert any("Label=openpc-root-a" in t for t in labels.values())
    assert any("Label=openpc-root-b" in t for t in labels.values())
    assert any("Label=openpc-var" in t for t in labels.values())
    # Slot B ships pre-sized so the first update never repartitions.
    assert any("Label=openpc-root-b" in t and "SizeMinBytes" in t for t in labels.values())


def test_bless_service_clears_the_boot_counter_and_ends_booting():
    text = (UNITS / "openpc-bless.service").read_text()
    assert "systemd-bless-boot good" in text
    # The LED leaves BOOTING on this flag.
    assert "touch /run/openpc/boot-complete" in text


def test_tpm2_enrollment_uses_narrow_pcrs_and_mints_a_recovery_key():
    text = (SCRIPTS / "enroll-tpm2").read_text()
    assert "--tpm2-pcrs=0+7" in text
    assert "--recovery-key" in text


# ---------------------------------------------------------------------------
# No console, minimal surface
# ---------------------------------------------------------------------------


def test_image_has_no_console():
    postinst = (OS / "image" / "mkosi.postinst").read_text()
    assert "getty@tty" in postinst
    assert "serial-getty@ttyS0.service" in postinst
    assert "PermitRootLogin no" in postinst


def test_image_ships_required_runtime_packages_only():
    conf = (OS / "image" / "mkosi.conf").read_text()
    for package in ("bluez", "network-manager", "avahi-daemon", "nftables", "xvfb", "chromium"):
        assert package in conf
    for compiler in ("gcc", "build-essential", "g++"):
        assert compiler not in conf, f"{compiler} has no place on an appliance"


def test_native_artefacts_must_be_verified_before_install():
    postinst = (OS / "image" / "mkosi.postinst").read_text()
    assert "sha256sum -c SHA256SUMS" in postinst
    assert "cua-driver" in postinst
    # A missing checksum file must abort the build, not warn.
    assert "SHA256SUMS is missing" in postinst


def test_build_image_checks_the_same_artefact_directory_as_postinst():
    """The pre-flight and the in-image check must look at one directory.

    They drifted once (scripts/artifacts vs image/artifacts), which made a
    correctly-populated build fail before mkosi ever ran.
    """
    build_image = (SCRIPTS / "build-image").read_text()
    postinst = (OS / "image" / "mkosi.postinst").read_text()
    assert 'os/image/artifacts' in build_image
    assert '$HERE/artifacts' not in build_image
    assert 'ARTIFACTS="$REPO/os/image/artifacts"' in postinst


@pytest.fixture
def artefact_dir(tmp_path):
    """A stand-in artefact directory with the three required files."""
    for name in ("cua-driver", "a-coder-cli", "llama.cpp-lfm3.tar.gz"):
        (tmp_path / name).write_bytes(name.encode() * 8)
    return tmp_path


def run_pin(artefact_dir, *args: str) -> subprocess.CompletedProcess[str]:
    script = SCRIPTS / "pin-artifacts"
    return subprocess.run(
        ["bash", str(script), *args, str(artefact_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_pin_artifacts_writes_sums_without_listing_itself(artefact_dir):
    result = run_pin(artefact_dir)
    assert result.returncode == 0, result.stderr
    lines = (artefact_dir / "SHA256SUMS").read_text().splitlines()
    assert len(lines) == 3
    assert sorted(line.split("  ")[1] for line in lines) == [
        "a-coder-cli",
        "cua-driver",
        "llama.cpp-lfm3.tar.gz",
    ]
    assert "SHA256SUMS" not in (artefact_dir / "SHA256SUMS").read_text()
    assert run_pin(artefact_dir, "--check").returncode == 0


def test_pin_artifacts_refuses_a_missing_or_changed_artefact(artefact_dir):
    assert run_pin(artefact_dir).returncode == 0
    (artefact_dir / "cua-driver").write_bytes(b"tampered")
    result = run_pin(artefact_dir, "--check")
    assert result.returncode != 0
    assert "cua-driver" in result.stderr

    (artefact_dir / "llama.cpp-lfm3.tar.gz").unlink()
    result = run_pin(artefact_dir)
    assert result.returncode != 0
    assert "llama.cpp-lfm3.tar.gz" in result.stderr


def test_agent_user_has_no_shell():
    postinst = (OS / "image" / "mkosi.postinst").read_text()
    assert "/usr/sbin/nologin" in postinst


def test_session_script_marks_attention_on_display_failure():
    text = (SCRIPTS / "openpc-session").read_text()
    assert "attention" in text
    assert "--remote-debugging-address=127.0.0.1" in text
