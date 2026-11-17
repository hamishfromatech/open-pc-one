"""Identity is configuration too: the OS has to say who it is, everywhere.

A branded image that reports Debian in one place and AmaaraOS in another is
worse than an unbranded one, so the checks here cover every surface a user or a
support engineer actually looks at: ``/etc/os-release``, ``/etc/issue``, the
wallpaper, the desktop defaults that point at it, and the unit descriptions the
boot log prints.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

OS = Path(__file__).resolve().parents[1]
OVERLAY = OS / "overlay"
OS_RELEASE = OVERLAY / "etc" / "os-release"
ISSUE = OVERLAY / "etc" / "issue"
WALLPAPER = OVERLAY / "usr" / "share" / "backgrounds" / "amaaraos" / "amaaraos-1920x1080.png"
XFCONF = OVERLAY / "etc" / "xdg" / "xfce4" / "xfconf" / "xfce-perchannel-xml" / "xfce4-desktop.xml"
VERSION_CMD = OVERLAY / "usr" / "local" / "bin" / "amaara-version"
UNITS = OVERLAY / "etc" / "systemd" / "system"
MKOSI = OS / "image" / "mkosi.conf"
POSTINST = OS / "image" / "mkosi.postinst"


def os_release() -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in OS_RELEASE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip().strip('"')
    return fields


def test_os_release_is_amaaraos():
    fields = os_release()
    assert fields["ID"] == "amaaraos"
    assert fields["PRETTY_NAME"].startswith("AmaaraOS ")
    # A branded appliance must not lie about what carries its security updates.
    assert fields["ID_LIKE"] == "debian"
    assert fields["DISTRIB_ID"] == "debian"
    assert fields["VENDOR_NAME"] == "The A-Tech Corporation"
    assert fields["AMAARAOS_DEVICE"] == "Open-PC One"


def test_version_numbers_agree_with_the_image():
    fields = os_release()
    mkosi = MKOSI.read_text()
    image_version = re.search(r"^ImageVersion=(\S+)$", mkosi, re.MULTILINE)
    assert image_version, "mkosi.conf must stamp an ImageVersion"
    assert image_version.group(1) == fields["VERSION"]
    assert re.match(r"^\d+\.\d+\.\d+$", fields["VERSION"]), "release versions are X.Y.Z"
    assert fields["VERSION_ID"] == ".".join(fields["VERSION"].split(".")[:2])
    assert "ImageId=amaaraos-one" in mkosi


def test_mkosi_cannot_undo_the_brand_os_release():
    # mkosi writes the distribution's own /etc/os-release during the build, so the
    # branded file has to be installed by the post-install script to win.
    script = POSTINST.read_text()
    assert "os/overlay/etc/os-release" in script
    assert '"$BUILDROOT/etc/os-release"' in script


def test_issue_names_the_os_and_says_how_to_get_in():
    text = ISSUE.read_text()
    assert "AmaaraOS" in text
    assert "openpc-engine pair-code" in text
    assert "amaara-version" in text


def test_amaara_version_is_executable_and_parses():
    assert VERSION_CMD.stat().st_mode & 0o111, "amaara-version must be executable"
    source = VERSION_CMD.read_text()
    assert "/etc/os-release" in source
    assert "/proc/cmdline" in source, "the A/B slot only exists in the kernel command line"
    completed = __import__("subprocess").run(["bash", "-n", str(VERSION_CMD)], capture_output=True)
    assert completed.returncode == 0, completed.stderr.decode()


def test_wallpaper_is_the_declared_size():
    # Parsed straight out of IHDR: the test suite should not need an image library
    # to check two integers.
    import struct

    data = WALLPAPER.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "wallpaper must be a PNG"
    assert data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1920, 1080)
    assert WALLPAPER.stat().st_size < 2 * 1024 * 1024, "a wallapper is not a photo library"


def test_desktop_defaults_point_at_the_branded_wallpaper():
    tree = ET.parse(XFCONF)
    paths = [node.get("value") for node in tree.iter("property") if node.get("name") == "image-path"]
    assert paths == [f"/usr/share/backgrounds/amaaraos/{WALLPAPER.name}"]
    # rgb a1 is the cream field: a missing PNG must not fall back to black.
    cream = [node.get("value") for node in tree.iter("value") if node.get("type") == "double"]
    assert cream[:3] == ["0.976", "0.957", "0.933"]


def test_no_unit_still_calls_this_open_pc_one():
    for unit in sorted(UNITS.glob("*.service")):
        description = next(
            (line.split("=", 1)[1] for line in unit.read_text().splitlines() if line.startswith("Description=")),
            "",
        )
        assert "Open-PC One" not in description, f"{unit.name} still describes the old product"
        assert description.startswith(("AmaaraOS", "Mark the AmaaraOS")), f"{unit.name}: {description}"
