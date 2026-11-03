"""``openpc-led`` command line."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .driver import MockLedDriver, SysfsLedDriver
from .service import LedService
from .sources import DEFAULT_RUN_DIR, FileSignalSource


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openpc-led", description=__doc__)
    parser.add_argument("--led-name", default="openpc:status", help="/sys/class/leds/<name> to drive")
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR), help="directory holding the flag files")
    parser.add_argument("--mock", action="store_true", help="log level writes instead of touching sysfs")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    if args.mock:
        driver = MockLedDriver()
    else:
        driver = SysfsLedDriver(args.led_name)

    service = LedService(driver=driver, source=FileSignalSource(Path(args.run_dir)))
    try:
        asyncio.run(service.run_forever())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
