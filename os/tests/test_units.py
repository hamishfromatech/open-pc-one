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
    # Predictable interface names (ens3 in QEMU, enp*/eno* on hardware) must be
    # treated as the LAN link, or DHCP replies are dropped at boot.
    assert '"en*"' in text


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


def test_repart_populates_the_esp_root_and_var_from_the_image_tree():
    repart = {p.name: p.read_text() for p in (OS / "image" / "mkosi.repart").glob("*.conf")}
    # mkosi does not add its default ESP rule when RepartDirectories= is used, so
    # without these the ESP is empty and QEMU boots the firmware shell.
    assert "CopyFiles=/boot:/" in repart["10-esp.conf"]
    assert "CopyFiles=/efi:/" in repart["10-esp.conf"]
    # '@' is not systemd-repart syntax: it resolves to a literal /@ directory
    # and leaves the root partition empty.
    assert "CopyFiles=/" in repart["20-root-a.conf"]
    assert "@/" not in repart["20-root-a.conf"]
    # /var is its own partition (systemd-gpt-auto-generator mounts it); it needs
    # the state directories, not an empty filesystem.
    assert "CopyFiles=/var:/" in repart["40-var.conf"]


def test_mkosi_conf_matches_mkosi_25_and_states_the_root_placeholder():
    conf = (OS / "image" / "mkosi.conf").read_text()
    content = conf.split("[Content]", 1)[1]
    # Bootable=/Hostname= belong to [Content] in mkosi 24+. Elsewhere they are
    # only a warning, and an ignored Bootable= silently yields no ESP.
    assert "Bootable=yes" in content
    assert "Hostname=openpc" in content
    assert "Bootable=" not in conf.split("[Content]", 1)[0]
    # mkosi 25 dies on setting names it does not know.
    assert "PackageManagerTrees" not in conf
    # The overlay is the image; mkosi does not read os/overlay on its own.
    assert "ExtraTrees=../overlay:/" in conf
    # mkosi.postinst needs the repository root, not os/image, at /work/src.
    assert "BuildSources=../.." in conf
    # Without the literal placeholder mkosi adds no root= to the cmdline.
    assert "root=PARTUUID" in conf
    assert "console=ttyS0" in conf


def test_postinst_finds_the_repository_root_in_the_mkosi_sandbox():
    postinst = (OS / "image" / "mkosi.postinst").read_text()
    # mkosi copies the script to /work/postinst, so BASH_SOURCE is not inside
    # the checkout and a dirname-derived REPO would be "/".
    assert "SRCDIR" in postinst
    assert 'REPO="$SRCDIR"' in postinst


def test_speech_is_preset_out_of_the_default_boot():
    # mkosi runs `systemctl preset-all`; a unit with [Install] is enabled unless
    # a preset says otherwise. openpc-speech Requires=docker.service, which the
    # v0.1 image does not ship.
    preset = OS / "overlay" / "etc" / "systemd" / "system-preset" / "50-openpc.preset"
    assert "disable openpc-speech.service" in preset.read_text()


def test_wheelhouse_carries_dependencies_for_the_image_interpreter():
    makefile = (OS.parent / "Makefile").read_text()
    # --no-index in mkosi.postinst only works if the runtime dependencies are in
    # the wheelhouse, resolved for trixie's Python 3.13 rather than the host's.
    assert "requirements-image.txt" in makefile
    assert "--python-version 3.13" in makefile
    requirements = (OS.parent / "requirements-image.txt").read_text()
    assert "websockets==15.0.1" in requirements
    assert "cryptography==44.0.0" in requirements


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


# --- regressions found by the THE-52 review of the pilot image ---------------
#
# Each of these was green in CI while broken on the appliance, because nothing
# asserted them. They are cheap, they are static, and they would have caught it.


def _section(path: Path, section: str) -> dict[str, list[str]]:
    """Parse a unit file into {section: [lines]}, comments dropped."""
    out: dict[str, list[str]] = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[([A-Za-z]+)\]$", line)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
            continue
        if current:
            out[current].append(line)
    return out


def test_start_limit_keys_are_in_unit_not_service():
    """systemd ignores StartLimit* in [Service] - the cap then does not exist.

    openpc-provision restarted 17 times on the pilot image because of this.
    """
    for unit in UNITS.glob("*.service"):
        sections = _section(unit, "x")
        for key in ("StartLimitIntervalSec", "StartLimitBurst"):
            in_service = any(line.startswith(key) for line in sections.get("Service", []))
            assert not in_service, (
                f"{unit.name}: {key} is in [Service], where systemd ignores it; "
                f"move it to [Unit]"
            )


def test_provisioning_has_a_restart_cap_in_unit():
    sections = _section(UNITS / "openpc-provision.service", "x")
    unit_lines = " ".join(sections.get("Unit", []))
    assert "StartLimitBurst=" in unit_lines
    assert "StartLimitIntervalSec=" in unit_lines


def test_bless_boot_points_at_the_real_binary_path():
    """/usr/bin/systemd-bless-boot does not exist on trixie; Debian ships it in
    /usr/lib/systemd. The old leading '-' hid the miss and the unit reported
    `active` while doing nothing."""
    text = (UNITS / "openpc-bless.service").read_text(encoding="utf-8")
    exec_lines = [
        line for line in text.splitlines() if line.strip().startswith("ExecStart=")
    ]
    assert exec_lines, "openpc-bless has no ExecStart"
    joined = " ".join(exec_lines)
    assert "/usr/lib/systemd/systemd-bless-boot" in joined
    assert "/usr/bin/systemd-bless-boot" not in joined
    assert "ExecStart=-" not in text, (
        "the '-' prefix hides a missing binary; the wrapper already treats a "
        "non-boot-counted entry as a no-op"
    )


def test_cua_driver_uses_the_unix_socket_interface():
    """cua-driver 0.33.3 is a unix-socket daemon. It has no --port: unknown flags
    are silently ignored, so `--port 8098 --display :0` was parsed as a
    positional JSON payload and the unit died on every start."""
    text = (UNITS / "cua-driver.service").read_text(encoding="utf-8")
    # Only the Exec lines matter; the comments explain the old broken argv.
    execs = [
        line.strip() for line in text.splitlines() if line.strip().startswith("Exec")
    ]
    joined = " ".join(execs)
    assert "serve" in joined
    assert "--socket" in joined
    assert "--port" not in joined, "0.33.3 has no --port; use serve --socket"
    assert "telemetry disable" in joined, (
        "cua-driver ships product telemetry on by default; a sovereign appliance "
        "must turn it off at start"
    )


def test_image_requirements_carry_the_ble_driver():
    """openpc-provision imports dbus_next on the BlueZ path. It was declared only
    as an optional extra, never shipped, and the unit failed 17 times."""
    text = (OS.parent / "requirements-image.txt").read_text(encoding="utf-8")
    assert "dbus-next" in text
