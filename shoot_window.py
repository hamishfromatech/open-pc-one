"""Capture one X window to a PNG and report the ink in its title band.

Screenshotting a screen region is useless here: the kiosk browser is fullscreen and
the window under test opens behind it, so a region shows the browser and every theme
variant then measures identically. This grabs the window itself.

    python3 shoot_window.py <window-id-hex> out.png [band-height]
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkX11  # noqa: E402


def main() -> int:
    window_id = int(sys.argv[1], 0)
    out = sys.argv[2]
    band = int(sys.argv[3]) if len(sys.argv) > 3 else 34

    window = GdkX11.X11Window.foreign_new_for_display(Gdk.Display.get_default(), window_id)
    if window is None:
        print("ink=-1 (no such window)")
        return 1
    width, height = window.get_width(), window.get_height()
    pixbuf = Gdk.pixbuf_get_from_window(window, 0, 0, width, height)
    if pixbuf is None:
        print("ink=-1 (capture failed)")
        return 1
    pixbuf.savev(out, "png", [], [])

    pixels = pixbuf.get_pixels()
    stride = pixbuf.get_rowstride()
    channels = pixbuf.get_n_channels()
    ink = 0
    for row in range(min(band, height)):
        base = row * stride
        for column in range(width):
            pixel = base + column * channels
            if pixels[pixel] + pixels[pixel + 1] + pixels[pixel + 2] < 250:
                ink += 1
    print(f"ink={ink} size={width}x{height} out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
