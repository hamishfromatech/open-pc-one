"""Window chrome and system defaults, checked against what xfwm4/xfconf expect.

The rules here come from upstream: xfwm4 loads one image per frame part and
button state (``<part>-<active|inactive>[-<state>].png``), and xfconf only reads
system-wide defaults from ``$XDG_CONFIG_DIRS/xfce4/xfconf/xfce-perchannel-xml``
and only honours ``locked`` there. Getting either wrong fails silently — the
window keeps the distribution's decoration, or the user's settings quietly win.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

OS = Path(__file__).resolve().parents[1]
THEME = OS / "overlay" / "usr" / "share" / "themes" / "Amaara" / "xfwm4"
XFCONF = OS / "overlay" / "etc" / "xdg" / "xfce4" / "xfconf" / "xfce-perchannel-xml"
GENERATOR = OS / "brand" / "make-xfwm-theme.py"

FRAME_PARTS = [
    "title-1", "title-2", "title-3", "title-4", "title-5",
    "top-left", "top-right", "bottom-left", "bottom-right",
    "top", "left", "right", "bottom",
]
BUTTONS = ["close", "hide", "maximize", "shade", "stick", "menu"]
BUTTON_STATES = ["active", "inactive", "prelight", "pressed"]


@pytest.mark.parametrize("part", FRAME_PARTS)
def test_every_frame_part_has_both_focus_states(part):
    for state in ("active", "inactive"):
        assert (THEME / f"{part}-{state}.png").is_file(), f"missing {part}-{state}.png"


@pytest.mark.parametrize("button", BUTTONS)
def test_every_button_has_every_state(button):
    for state in BUTTON_STATES:
        assert (THEME / f"{button}-{state}.png").is_file(), f"missing {button}-{state}.png"


def test_themerc_has_the_keys_the_theme_relies_on():
    fields = {}
    for line in (THEME / "themerc").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()

    assert fields["button_layout"] == "CHM|", "close, hide, maximise on the left"
    assert fields["title_alignment"] == "center"
    assert fields["title_font"].startswith("Inter")
    for required in ("active_text_color", "inactive_text_color", "full_width_title",
                     "title_vertical_offset_active", "title_vertical_offset_inactive"):
        assert required in fields, f"themerc is missing {required}"


def test_the_generator_writes_every_part_these_tests_expect():
    # The PNGs are committed so the image needs no Pillow, but a regeneration must
    # not silently drop a part: the generator has to name each one.
    source = GENERATOR.read_text()
    for part in ("title-1", "top-left", "top-right", "bottom-left", "bottom-right",
                 "top-", "left-", "right-", "bottom-", "close", "hide", "maximize"):
        assert part in source, f"generator never writes {part}"


def test_defaults_live_where_xfconf_reads_them():
    # $XDG_CONFIG_DIRS entry, not a user config dir: an appliance has no user who
    # could ever write ~/.config/xfce4/xfconf.
    for channel in ("xfwm4.xml", "xfce4-desktop.xml"):
        assert (XFCONF / channel).is_file(), f"{channel} must be a system-wide default"


def test_brand_properties_are_locked_for_everyone():
    wm = ET.parse(XFCONF / "xfwm4.xml").getroot()
    locked = {
        node.get("name"): node.get("locked")
        for node in wm.iter("property")
        if node.get("locked")
    }
    assert locked.get("theme") == "*", "the window theme is the OS, not a preference"
    assert locked.get("button_layout") == "*"
    assert locked.get("title_font") == "*"

    desk = ET.parse(XFCONF / "xfce4-desktop.xml").getroot()
    images = [node for node in desk.iter("property") if node.get("name") == "image-path"]
    assert images and images[0].get("locked") == "*", "the wallpaper is the brand"


def test_every_system_channel_file_is_valid_xml():
    for path in sorted(XFCONF.glob("*.xml")):
        ET.parse(path)  # raises on malformed XML, which xfconf would reject
