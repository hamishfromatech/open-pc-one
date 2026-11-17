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


def test_the_lock_cover_owns_the_whole_screen():
    source = SHELL.read_text()
    # A lock screen with the bar and dock on top of it is a decorated desktop,
    # not a locked one: the shell hides its own surfaces while the cover is up.
    assert "class LockScreen" in source
    assert "_cover_surfaces" in source and "_reveal_surfaces" in source
    assert "surface.hide()" in source
    assert "PRESS ENTER" in source.upper()
    # and it must never be able to swallow the session: no grab, no modal loop
    assert "Gtk.main()" in source  # one loop only, shared with the shell


def test_the_settings_app_is_ours_and_reads_the_running_system():
    settings = SHELL.parent / "amaara-settings"
    assert settings.is_file(), "amaara-settings must ship next to the shell"
    assert settings.stat().st_mode & 0o111
    source = settings.read_text()
    # It has to describe this machine, not XFCE: OS identity, setup state, the
    # A/B slot and the model manifest are all read, not invented.
    for probe in ("/etc/os-release", "/proc/cmdline", "setup/state.json", "models.json"):
        assert probe in source, f"settings never reads {probe}"
    # The panes are ours, about this machine, not XFCE's vocabulary.
    for pane in ("About this computer", "Display", "Network", "Audio", "Software",
                 "Agent", "Updates", "A/B root slots"):
        assert pane in source, f"settings has no {pane!r} surface"
    # The dock must open ours (the string may still appear in a comment explaining
    # why it is not xfce4-settings-manager).
    shell = SHELL.read_text()
    assert '"amaara-settings"' in shell
    assert "xfce4-settings-manager" not in shell.split("APPS = [")[1].split("]")[0]


def test_the_settings_app_reads_the_state_file_s_own_field_names():
    # The setup state is written from a dataclass, so the keys are snake_case.
    # Reading camelCase showed an em dash where a real device id existed.
    source = (SHELL.parent / "amaara-settings").read_text()
    for key in ("device_id", "license_tier", "license_batch", "phase"):
        assert key in source, f"settings never reads {key}"
    for wrong in ("deviceId", "licenseTier", "licenseBatch"):
        assert wrong not in source, f"settings reads a key the state file never writes: {wrong}"


def test_the_launcher_dismisses_when_focus_leaves_but_the_lock_does_not():
    source = SHELL.read_text()
    lock = source.split("class LockScreen")[1].split("class Launcher")[0]
    launcher = source.split("class Launcher")[1]
    # An overlay left open behind the app you just launched is the classic
    # launcher bug; a lock screen that dismisses when it loses focus is worse.
    assert "_on_focus_out" in launcher
    assert "_on_focus_out" not in lock
    assert "self.hide()" in launcher.split("def _on_focus_out")[1].split("def ")[0]


def test_settings_never_renders_an_empty_separator():
    source = (SHELL.parent / "amaara-settings").read_text()
    assert "not activated" in source, "an empty licence must say so, not print a bare separator"
    assert "if tier or batch" in source, "the empty case must be handled, not concatenated"
