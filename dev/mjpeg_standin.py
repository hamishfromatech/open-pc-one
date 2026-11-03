"""A stand-in for the :8090 desktop stream.

The real appliance serves the live X11 desktop from the Open-PC agent server on
:8090. That server is not part of this repository, so the Docker development
path serves a generated MJPEG stream with the same shape:

    GET /v1/mjpeg   multipart/x-mixed-replace; boundary=frame, image/jpeg parts
    GET /healthz    {"status": "ok", "standin": true}

It is obviously not a desktop on purpose. If a client renders this, its MJPEG
transport is working; swap in the real server for anything else.
"""

from __future__ import annotations

import argparse
import io
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 640, 360
BG = (12, 14, 18)
FG = (0, 200, 220)
DIM = (80, 90, 100)
BOUNDARY = "frame"


def render(frame_number: int) -> bytes:
    """One JPEG frame: a moving bar, a frame counter and a clear label."""
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)

    x = (frame_number * 8) % (WIDTH + 120)
    draw.rectangle([x - 120, 0, x, HEIGHT], fill=(18, 40, 46))
    draw.rectangle([x - 4, 0, x, HEIGHT], fill=FG)

    draw.text((24, 24), "OPEN-PC  :8090  MJPEG STAND-IN", fill=FG)
    draw.text((24, 48), "not the real desktop - transport check only", fill=DIM)
    draw.text((24, HEIGHT - 48), f"frame {frame_number}", fill=DIM)
    draw.rectangle([0, 0, WIDTH - 1, HEIGHT - 1], outline=DIM)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=70)
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fps = 10.0

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/healthz":
            body = b'{"status":"ok","standin":true}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path not in ("/", "/v1/mjpeg", "/stream"):
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        frame_number = 0
        try:
            while True:
                payload = render(frame_number)
                self.wfile.write(f"--{BOUNDARY}\r\n".encode())
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(payload)}\r\n\r\n".encode())
                self.wfile.write(payload)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                frame_number += 1
                time.sleep(1.0 / self.fps)
        except (BrokenPipeError, ConnectionResetError):
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mjpeg-standin", description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0:8090", help="host:port (default 0.0.0.0:8090)")
    parser.add_argument("--fps", type=float, default=10.0)
    args = parser.parse_args(argv)

    host, _, port = args.bind.rpartition(":")
    Handler.fps = max(args.fps, 0.5)
    server = ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler)
    print(f"mjpeg stand-in on http://{host or '0.0.0.0'}:{port}/v1/mjpeg", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
