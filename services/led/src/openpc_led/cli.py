"""``openpc-led`` command line."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .driver import MockLedDriver, SysfsLedDriver
from .service import LedService
from .sources import DEFAULT_RUN_DIR, FileSignalSource


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openpc-led", description=__doc__)
    # Env fallbacks exist for VMs and CI, where there is no power-button ring:
    # a drop-in sets OPENPC_LED_MOCK=1 so the service still exercises the loop.
    parser.add_argument(
        "--led-name",
        default=os.environ.get("OPENPC_LED_NAME", "openpc:status"),
        help="/sys/class/leds/<name> to drive (env: OPENPC_LED_NAME)",
    )
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR), help="directory holding the flag files")
    parser.add_argument(
        "--mock",
        action="store_true",
        default=_env_true("OPENPC_LED_MOCK"),
        help="log level writes instead of touching sysfs (env: OPENPC_LED_MOCK=1)",
    )
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
