"""The shell is part of the OS surface, so its wiring is a test, not a hope."""

from __future__ import annotations

import re
from pathlib import Path

OS = Path(__file__).resolve().parents[1]
OVERLAY = OS / "overlay"
SHELL = OVERLAY / "usr" / "local" / "bin" / "amaara-shell"
SESSION = OS / "scripts" / "openpc-session"
MKOSI = OS / "image" / "mkosi.conf"
GTK = OVERLAY / "etc" / "xdg" / "gtk-3.0" / "settings.ini"


def test_shell_is_executable_python():
    assert SHELL.stat().st_mode & 0o111
    source = SHELL.read_text()
    assert source.startswith("#!/usr/bin/env python3")
    compile(source, str(SHELL), "exec")  # parses, imports are runtime


def test_shell_uses_the_brand_tokens():
    source = SHELL.read_text()
    for token in ("#F9F4EE", "#0B0908", "#D72E35"):
        assert token in source, f"{token} missing from the shell"


def test_the_session_starts_the_shell_and_not_a_panel():
    session = SESSION.read_text()
    assert "amaara-shell &" in session
    packages = [line.strip() for line in MKOSI.read_text().splitlines()
                if line.startswith("    ") and not line.strip().startswith("#")]
    assert "xfce4-panel" not in packages, "the default XFCE panel would double up with the shell"
    assert "amaara-shell" in session


def test_the_image_ships_what_the_shell_imports_and_draws():
    packages = MKOSI.read_text()
    for needed in ("python3-gi", "gir1.2-gtk-3.0", "fonts-inter", "papirus-icon-theme",
                   "xfdesktop4", "xdotool"):
        assert re.search(rf"^\s+{re.escape(needed)}$", packages, re.MULTILINE), (
            f"{needed} is missing from the image package list"
        )


def test_gtk_defaults_name_the_brand_fonts_and_icons():
    settings = GTK.read_text()
    assert "gtk-font-name=Inter" in settings
    assert "gtk-icon-theme-name=Papirus" in settings
