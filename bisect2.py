#!/usr/bin/env python3
"""Bisect the Amaara themerc against a *window* capture.

Earlier attempts screenshot a screen region: the kiosk browser is fullscreen, the
window under test opens behind it, and every variant measured the same thing
(zero). This captures the window itself, so the numbers mean something.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

SHIPPED = Path("/usr/share/themes/Amaara/xfwm4/themerc").read_text()
DEFAULT = Path("/usr/share/themes/Default/xfwm4/themerc").read_text()
COLOURS = """active_text_color=active_text_color_2
inactive_text_color=inactive_text_color_2
active_text_shadow_color=active_hilight_2
inactive_text_shadow_color=inactive_hilight_2
"""
PROBE = Path("/usr/share/themes/ProbeA/xfwm4/themerc")


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=False, capture_output=True, **kwargs)


def theme(value: str) -> None:
    run(["xfconf-query", "-c", "xfwm4", "-p", "/general/theme", "-s", value])


def measure(label: str, themerc: str) -> int:
    PROBE.write_text(themerc)
    theme("Default")
    time.sleep(0.5)
    theme("ProbeA")
    time.sleep(1.2)
    run(["pkill", "-f", "/usr/local/bin/amaara-settings"])
    time.sleep(0.6)
    subprocess.Popen(["amaara-settings"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3.0)
    ids = run(["xdotool", "search", "--name", "^Settings$"], text=True).stdout.split()
    if not ids:
        print(f"{label:34} no window")
        return -1
    out = f"/tmp/win_{label}.png"
    done = run(["python3", "/tmp/shoot_window.py", ids[-1], out, "34"], text=True)
    ink = 0
    for token in done.stdout.split():
        if token.startswith("ink="):
            ink = int(token.split("=")[1])
    print(f"{label:34} ink={ink}", flush=True)
    return ink


def without(*prefixes: str) -> str:
    kept = [
        line
        for line in SHIPPED.splitlines()
        if not any(line.startswith(prefix) for prefix in prefixes)
    ]
    return "\n".join(kept) + "\n"


os.environ.setdefault("DISPLAY", ":0")
measure("0-shipped", SHIPPED)
measure("1-default+colours", DEFAULT + COLOURS)
measure("2-no-title_font", without("title_font"))
measure("3-no-title_alignment", without("title_alignment"))
measure("4-no-title_shadow", without("title_shadow"))
measure("5-no-shadow_delta", without("shadow_delta", "shadow_opacity"))
measure("6-no-maximized_offset", without("maximized_offset"))
measure("7-no-offsets", without("title_vertical_offset", "button_offset", "button_spacing"))
theme("Amaara")
