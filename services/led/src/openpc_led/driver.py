"""LED drivers: sysfs on the appliance, in-memory for tests."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

log = logging.getLogger("openpc.led.driver")

LED_CLASS_ROOT = Path("/sys/class/leds")


class LedDriver(Protocol):
    def set_brightness(self, value: int) -> None: ...

    def close(self) -> None: ...


class SysfsLedDriver:
    """Write ``/sys/class/leds/<name>/brightness``.

    The kernel LED class handles the PWM; this process only sets a level, which
    keeps the pattern loop cheap on an N150.
    """

    def __init__(self, led_name: str, root: Path = LED_CLASS_ROOT) -> None:
        self.path = Path(root) / led_name / "brightness"
        if not self.path.exists():
            raise FileNotFoundError(
                f"no LED named {led_name!r} under {root}; set OPENPC_LED_NAME to the "
                "directory the device tree exposes for the power-button ring"
            )
        self._fd: int | None = None
        # Open once and seek to 0 for every write: opening per frame would be
        # thousands of syscalls a minute.
        self._fd = os.open(self.path, os.O_WRONLY)

    def set_brightness(self, value: int) -> None:
        if self._fd is None:
            return
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, str(max(0, min(255, value))).encode())

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class MockLedDriver:
    """Records the levels written, and the state transitions, for tests."""

    def __init__(self) -> None:
        self.values: list[int] = []
        self.closed = False

    def set_brightness(self, value: int) -> None:
        self.values.append(value)

    def close(self) -> None:
        self.closed = True
