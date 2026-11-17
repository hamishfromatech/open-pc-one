#!/usr/bin/env python3
"""Bisect which themerc key makes xfwm4 drop the window title text.

Runs inside the session: for each candidate key, write a themerc, re-apply the
theme, relaunch a window, screenshot the title band and count ink pixels. Zero ink
means the title text did not draw.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

TMP = Path("/tmp/bisect")
TMP.mkdir(exist_ok=True)
THEME_DIR = Path("/usr/share/themes/ProbeA/xfwm4")
DEFAULT_THEMERC = Path("/usr/share/themes/Default/xfwm4/themerc").read_text()
OUR_COLOURS = (
    "active_text_color=active_text_color_2\n"
    "inactive_text_color=inactive_text_color_2\n"
    "active_text_shadow_color=active_hilight_2\n"
    "inactive_text_shadow_color=inactive_hilight_2\n"
)


def run(argv: list[str]) -> None:
    subprocess.run(argv, check=False, capture_output=True)


def xfconf(value: str) -> None:
    run(["xfconf-query", "-c", "xfwm4", "-p", "/general/theme", "-s", value])


def apply_theme(themerc: str, label: str) -> None:
    (THEME_DIR / "themerc").write_text(themerc)
    xfconf("Default")
    time.sleep(0.6)
    xfconf("ProbeA")
    time.sleep(1.2)
    run(["pkill", "-f", "^amaara-settings"])
    time.sleep(0.6)
    subprocess.Popen(["amaara-settings"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.5)
    # the kiosk browser is fullscreen: without activating, the window we are
    # measuring opens behind it and every variant looks identical (all zero)
    ids = subprocess.run(["xdotool", "search", "--name", "^Settings$"],
                         capture_output=True, text=True).stdout.split()
    if ids:
        run(["xdotool", "windowactivate", "--sync", ids[-1]])
    time.sleep(1.0)


def ink(label: str) -> int:
    out = TMP / f"{label}.png"
    run(["python3", "/tmp/shoot.py", str(out)])
    code = (
        "from PIL import Image;"
        f"im=Image.open(r'{out}').convert('RGB').crop((520,226,1412,266));"
        "print(sum(1 for p in list(im.getdata()) if sum(p)<250))"
    )
    done = subprocess.run(["python3", "-c", code], capture_output=True, text=True)
    return int(done.stdout.strip() or 0)


def variant(label: str, themerc: str) -> None:
    apply_theme(themerc, label)
    print(f"{label:28} ink={ink(label)}", flush=True)


def default_minus(*drop: str) -> str:
    kept = [
        line
        for line in DEFAULT_THEMERC.splitlines()
        if line.strip() and not any(line.startswith(key) for key in drop)
    ]
    return "\n".join(kept) + "\n" + OUR_COLOURS


os.environ.setdefault("DISPLAY", ":0")
variant("v0-default", DEFAULT_THEMERC)
variant("v1-default+colours", DEFAULT_THEMERC + OUR_COLOURS)
variant("v2-minus-frame_border_top", default_minus("frame_border_top"))
variant("v3-minus-show_app_icon", default_minus("show_app_icon"))
variant("v4-minus-horizontal_offset", default_minus("title_horizontal_offset"))
variant("v5-minus-shadows", default_minus("shadow_delta", "shadow_opacity"))
variant("v6-ours-layout", (DEFAULT_THEMERC + OUR_COLOURS).replace("button_offset=0", "button_offset=6")
        .replace("button_spacing=2", "button_spacing=6") + "button_layout=CHM|\n")
xfconf("Amaara")
