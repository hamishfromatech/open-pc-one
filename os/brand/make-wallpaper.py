#!/usr/bin/env python3
"""Generate the AmaaraOS desktop wallpaper.

Brand palette is taken from the A-Tech club banner: cream field, near-black ink,
one crimson accent, faint diagonal pinstripes. Nothing photographic is copied
from the banner; this is a flat composition in those colours.

    python3 os/brand/make-wallpaper.py --out os/overlay/usr/share/backgrounds/amaaraos/amaaraos-1920x1080.png

Fonts are looked up in order and the first hit wins, so the same command works
on a Debian build host and on a Windows dev box. The output is committed; this
script exists so the wallpaper can be regenerated, not so it is generated at
build time.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# --- brand tokens (see docs/BRAND.md) ---------------------------------------
CREAM = (249, 244, 238)
CREAM_SHADE = (239, 228, 219)  # pinstripe
INK = (11, 9, 8)
RED = (215, 46, 53)
RED_DEEP = (168, 32, 42)

SERIF_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "C:/Windows/Fonts/georgiab.ttf",
    "C:/Windows/Fonts/constanb.ttf",
    "C:/Windows/Fonts/timesbd.ttf",
]
SERIF_ITALIC_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
    "C:/Windows/Fonts/georgiai.ttf",
    "C:/Windows/Fonts/constani.ttf",
    "C:/Windows/Fonts/timesi.ttf",
]
SANS_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def first_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for name in candidates:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    raise SystemExit(f"no font found in {candidates}")


def tracked(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, tracking: int) -> int:
    """Draw letter-spaced text (PIL has no tracking); returns the width used."""
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + tracking
    return int(x - xy[0])


def build(width: int, height: int, subtitle: str) -> Image.Image:
    img = Image.new("RGB", (width, height), CREAM)
    draw = ImageDraw.Draw(img)

    # Diagonal pinstripes, as faint as the banner's paper texture.
    stripe_gap = 26
    for offset in range(-height, width + height, stripe_gap):
        draw.line([(offset, 0), (offset - height, height)], fill=CREAM_SHADE, width=1)

    pad_x = int(width * 0.083)
    pad_y = int(height * 0.115)

    # Accent rule + tracked label, the banner's masthead.
    bar_w, bar_h = int(width * 0.043), 6
    draw.rectangle([pad_x, pad_y, pad_x + bar_w, pad_y + bar_h], fill=RED)
    label_font = first_font(SANS_CANDIDATES, max(14, width // 96))
    tracked(
        draw,
        (pad_x + bar_w + width // 80, pad_y - 6),
        "THE A-TECH CORPORATION",
        label_font,
        INK,
        tracking=max(3, width // 320),
    )

    # Wordmark. The real brand face is a licensed display serif; this is the
    # closest thing on a stock Debian box.
    word_font = first_font(SERIF_CANDIDATES, int(height * 0.175))
    word = "AmaaraOS"
    draw.text((pad_x, int(height * 0.30)), word, font=word_font, fill=INK)

    # The italic crimson line, straight from the banner's hierarchy.
    accent_font = first_font(SERIF_ITALIC_CANDIDATES, int(height * 0.072))
    draw.text((pad_x + int(width * 0.006), int(height * 0.545)), subtitle, font=accent_font, fill=RED)

    # Version + device, quiet, bottom-left.
    small = first_font(SANS_CANDIDATES, max(15, width // 100))
    tracked(
        draw,
        (pad_x, int(height * 0.855)),
        "AMAARAOS 1.0  ·  OPEN-PC ONE",
        small,
        (110, 96, 88),
        tracking=max(2, width // 480),
    )

    # Red footer rule, the way the banner is cut off at the bottom.
    draw.rectangle([0, height - max(6, height // 90), width, height], fill=RED)
    return img


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument(
        "--subtitle",
        default="your agent's own computer",
        help="italic crimson line under the wordmark",
    )
    args = parser.parse_args()

    img = build(args.width, args.height, args.subtitle)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    img.save(args.out, "PNG", optimize=True)
    print(f"wrote {args.out} ({img.size[0]}x{img.size[1]}, {args.out.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()
