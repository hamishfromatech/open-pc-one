"""The :8090 MJPEG stand-in must serve a real multipart MJPEG stream.

It is a development aid, not the product, but a client that cannot read it would
be debugging the wrong layer. Pillow is a Docker-only dependency, so this skips
on a native `make venv` (which is fine - the transport is exercised by hand).
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

pytest.importorskip("PIL")
STANDIN = Path(__file__).resolve().parents[2] / "dev" / "mjpeg_standin.py"


def load_module():
    spec = importlib.util.spec_from_file_location("mjpeg_standin", STANDIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_render_produces_a_jpeg() -> None:
    module = load_module()
    frame = module.render(0)
    assert frame.startswith(b"\xff\xd8"), "missing JPEG SOI"
    assert frame.rstrip().endswith(b"\xff\xd9"), "missing JPEG EOI"

    from PIL import Image

    image = Image.open(io.BytesIO(frame))
    assert image.size == (module.WIDTH, module.HEIGHT)
    assert image.mode == "RGB"


def test_consecutive_frames_differ() -> None:
    module = load_module()
    assert module.render(0) != module.render(10), "the bar must actually move"
