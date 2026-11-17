"""``amaara-companion`` — connect to a box and open the window."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .client import Companion
from .ui import Session, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amaara-companion", description=__doc__)
    parser.add_argument("--version", action="version", version=f"amaara-companion {__version__}")
    parser.add_argument("--host", required=True, help="host:port of the box, e.g. openpc.local:8095")
    parser.add_argument("--pair", default="", help="one-time pairing code (first run only)")
    parser.add_argument("--ui", default="127.0.0.1:8123", help="where to serve the window")
    parser.add_argument("--stats-every", type=float, default=2.0,
                        help="seconds between resource polls (0 disables)")
    args = parser.parse_args(argv)

    companion = Companion(args.host)
    session = Session(companion)
    companion.on_state = session.on_state
    companion.on_frame = session.on_frame

    try:
        appliance = companion.connect(args.pair or None)
    except Exception as exc:
        print(f"amaara-companion: cannot reach {args.host}: {exc}", file=sys.stderr)
        return 2

    session.appliance = appliance
    session.pin = companion.pin
    print(f"amaara-companion: connected to {appliance.get('name', 'the box')} "
          f"(pin {companion.pin[:23]}…)" if companion.pin else "amaara-companion: connected")
    companion.subscribe("main")

    if args.stats_every > 0:
        import threading
        import time

        def poll() -> None:
            while True:
                try:
                    companion.stats()
                except Exception:
                    return
                time.sleep(args.stats_every)

        threading.Thread(target=poll, daemon=True).start()

    host, _, port = args.ui.rpartition(":")
    print(f"amaara-companion: window on http://{host or '127.0.0.1'}:{port or 8123}")
    try:
        serve(session, host or "127.0.0.1", int(port or 8123))
    finally:
        companion.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
