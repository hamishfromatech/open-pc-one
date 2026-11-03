"""The single systemd service that owns the LED.

Signals are sampled every couple of seconds; the pattern is rendered at 20 Hz.
Writing a sysfs brightness twice a frame when nothing changed would be pure
noise, so a write only happens when the level moves by more than a dead-band.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .driver import LedDriver
from .patterns import MAX_BRIGHTNESS, LedState, Signals, brightness, resolve_state
from .sources import FileSignalSource

log = logging.getLogger("openpc.led.service")

FRAME_INTERVAL = 0.05  # 20 Hz: smooth enough for a 2.4 s breathe
SIGNALS_INTERVAL = 2.0
DEAD_BAND = 2


@dataclass(slots=True)
class _Render:
    state: LedState
    value: int


class LedService:
    def __init__(
        self,
        driver: LedDriver,
        source: FileSignalSource,
        clock=time.monotonic,
        frame_interval: float = FRAME_INTERVAL,
        signals_interval: float = SIGNALS_INTERVAL,
    ) -> None:
        self.driver = driver
        self.source = source
        self.clock = clock
        self.frame_interval = frame_interval
        self.signals_interval = signals_interval
        self.signals = Signals()
        self.state = LedState.IDLE
        self._last = _Render(state=LedState.IDLE, value=-999)
        self._signals_at = -1e9

    # -- one frame (unit-testable) -----------------------------------------

    async def tick(self, now: float | None = None) -> _Render:
        now = self.clock() if now is None else now
        if now - self._signals_at >= self.signals_interval:
            self.signals = await self.source.read()
            self._signals_at = now
            new_state = resolve_state(self.signals)
            if new_state is not self.state:
                log.info(
                    "led state",
                    extra={"event": new_state.value, "detail": f"network={self.signals.network_present} busy={self.signals.engine_busy} attention={self.signals.attention}"},
                )
                self.state = new_state
                self._last = _Render(state=new_state, value=-999)  # force first write

        value = brightness(self.state, now)
        moved = abs(value - self._last.value) > DEAD_BAND
        # A transition to fully on/off must land immediately even if the delta
        # from the previous frame is small (e.g. 253 -> 255 is not).
        edge = value in (0, MAX_BRIGHTNESS) and value != self._last.value
        if moved or edge:
            self.driver.set_brightness(value)
            self._last = _Render(state=self.state, value=value)
        return self._last

    # -- loop --------------------------------------------------------------

    async def run_forever(self) -> None:
        log.info("led service started", extra={"event": "start"})
        try:
            while True:
                await self.tick()
                await asyncio.sleep(self.frame_interval)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                self.driver.set_brightness(0)
            finally:
                self.driver.close()
            log.info("led service stopped", extra={"event": "stop"})
