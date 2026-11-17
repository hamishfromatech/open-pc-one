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


def test_the_launcher_reads_real_desktop_entries():
    source = SHELL.read_text()
    # A launcher that hard-codes an app list is a mock. It must read the entries
    # the system installs, skip ones for software that is not present, and drop
    # field codes (a %U left in argv makes the launch a silent no-op).
    assert "/usr/share/applications" in source
    assert "NoDisplay" in source and "Hidden" in source
    assert "FIELD_CODES" in source and "shutil.which(argv[0])" in source


def test_the_wordmark_opens_the_launcher():
    source = SHELL.read_text()
    assert 'word.connect("clicked"' in source
    assert "self.launcher.toggle" in source
    assert "Gdk.KEY_Escape" in source, "an overlay with no way out is a trap"


def test_the_stylesheet_only_uses_properties_gtk_understands():
    # GTK's CSS is not web CSS: a single unsupported property makes
    # CssProvider.load_from_data() raise, and the whole shell fails to start (it
    # happened with text-transform, which took the bar and dock down with it).
    import re

    css = re.search(r'BAR_CSS = f"""(.*?)"""', SHELL.read_text(), re.DOTALL)
    assert css, "the stylesheet is a BAR_CSS string"
    forbidden = ("text-transform", "display: flex", "display: grid", "flex-direction",
                 "justify-content", "align-items: center;}")  # last one is a typo guard
    for prop in forbidden:
        assert prop not in css.group(1), f"{prop} is not valid GTK CSS"
