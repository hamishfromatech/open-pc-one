"""Patterns and the LED service loop."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openpc_led.driver import MockLedDriver  # noqa: E402
from openpc_led.patterns import (  # noqa: E402
    MAX_BRIGHTNESS,
    LedState,
    Signals,
    brightness,
    colour,
    is_lit,
    resolve_state,
)
from openpc_led.service import LedService  # noqa: E402


class FakeSource:
    def __init__(self, signals: Signals) -> None:
        self.signals = signals
        self.reads = 0

    async def read(self) -> Signals:
        self.reads += 1
        return self.signals


# ---------------------------------------------------------------------------
# Resolution and priority
# ---------------------------------------------------------------------------


def test_booting_outranks_everything():
    # Nothing else is knowable during boot (APP-ARCHITECTURE.md §3).
    assert resolve_state(Signals(booting=True, network_present=False, attention=True)) is LedState.BOOTING


def test_booting_is_dim_and_distinct_from_idle():
    samples = [brightness(LedState.BOOTING, t / 100) for t in range(0, 300)]
    assert max(samples) < brightness(LedState.IDLE, 1.2)  # dimmer than idle
    assert min(samples) > 0
    assert brightness(LedState.BOOTING, 0.0) == brightness(LedState.BOOTING, 3.0)


def test_priority_attention_beats_everything():
    assert resolve_state(Signals(network_present=False, engine_busy=True, attention=True)) is LedState.ATTENTION


def test_no_network_beats_working():
    assert resolve_state(Signals(network_present=False, engine_busy=True)) is LedState.NO_NETWORK


def test_working_beats_idle():
    assert resolve_state(Signals(network_present=True, engine_busy=True)) is LedState.WORKING
    assert resolve_state(Signals(network_present=True, engine_busy=False)) is LedState.IDLE


def test_colours_match_the_spec():
    assert colour(LedState.BOOTING) == (72, 72, 72)  # dim white
    assert colour(LedState.NO_NETWORK) == (255, 150, 0)  # amber
    assert colour(LedState.IDLE) == (0, 96, 255)  # blue
    assert colour(LedState.WORKING) == (0, 224, 255)  # cyan
    assert colour(LedState.ATTENTION) == (255, 0, 0)  # red


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------


def test_no_network_is_solid_amber_forever():
    for t in (0.0, 0.1, 1.0, 3600.0, 12345.678):
        assert brightness(LedState.NO_NETWORK, t) == MAX_BRIGHTNESS


def test_idle_breathes_and_never_goes_dark():
    samples = [brightness(LedState.IDLE, t / 100) for t in range(0, 240)]
    assert max(samples) == MAX_BRIGHTNESS
    assert min(samples) > 0  # a dark ring would read as a dead box
    # One full cycle in 2.4 s: brightest at the half period.
    assert brightness(LedState.IDLE, 0.0) < 40
    assert brightness(LedState.IDLE, 1.2) == MAX_BRIGHTNESS
    assert brightness(LedState.IDLE, 2.4) == brightness(LedState.IDLE, 0.0)


def test_working_pulses_at_4hz():
    on = sum(1 for i in range(250) if is_lit(LedState.WORKING, i / 1000))
    # 4 Hz square over 0.25 s of samples: half lit.
    assert 100 <= on <= 150
    assert brightness(LedState.WORKING, 0.0) == MAX_BRIGHTNESS
    assert brightness(LedState.WORKING, 0.2) == 0


def test_attention_double_blinks_then_pauses():
    lit = [t / 1000 for t in range(0, 2000) if is_lit(LedState.ATTENTION, t / 1000)]
    # Two bursts of ~120 ms inside a 2 s cycle.
    assert 200 < len(lit) < 320
    assert any(t < 0.12 for t in lit)
    assert any(0.28 <= t < 0.40 for t in lit)
    assert not any(1.0 <= t < 2.0 for t in lit)


def test_patterns_are_periodic():
    for state, period in (
        (LedState.BOOTING, 3.0),
        (LedState.IDLE, 2.4),
        (LedState.WORKING, 0.25),
        (LedState.ATTENTION, 2.0),
    ):
        for t in (0.0, 0.037, 0.5):
            assert brightness(state, t) == brightness(state, t + period)


# ---------------------------------------------------------------------------
# Service loop
# ---------------------------------------------------------------------------


async def test_tick_writes_only_on_meaningful_change():
    driver = MockLedDriver()
    source = FakeSource(Signals(network_present=True))
    service = LedService(driver, source, signals_interval=0.0)
    await service.tick(now=0.0)
    first = len(driver.values)
    assert first >= 1
    # Two frames close together produce no extra write (dead-band).
    await service.tick(now=0.01)
    await service.tick(now=0.02)
    assert len(driver.values) == first


async def test_state_change_forces_a_write():
    driver = MockLedDriver()
    source = FakeSource(Signals(network_present=True))
    service = LedService(driver, source, signals_interval=0.0)
    await service.tick(now=0.0)
    source.signals = Signals(network_present=False)
    render = await service.tick(now=0.05)
    assert render.state is LedState.NO_NETWORK
    assert driver.values[-1] == MAX_BRIGHTNESS


async def test_attention_flag_wins_in_the_service():
    driver = MockLedDriver()
    source = FakeSource(Signals(attention=True))
    service = LedService(driver, source, signals_interval=0.0)
    render = await service.tick(now=0.0)
    assert render.state is LedState.ATTENTION


async def test_full_on_is_written_even_below_the_dead_band():
    driver = MockLedDriver()
    source = FakeSource(Signals(network_present=True))
    service = LedService(driver, source, signals_interval=0.0)
    # Idle at t=1.1 s is already near full; the next frame must still land 255.
    await service.tick(now=1.1)
    await service.tick(now=1.2)
    assert driver.values[-1] == MAX_BRIGHTNESS


async def test_signals_are_not_re_read_every_frame():
    driver = MockLedDriver()
    source = FakeSource(Signals())
    service = LedService(driver, source, signals_interval=1.0)
    for i in range(25):
        await service.tick(now=i * 0.05)
    assert source.reads == 2  # t=0 and t=1.0


async def test_loop_turns_the_led_off_on_shutdown():
    driver = MockLedDriver()
    source = FakeSource(Signals())
    service = LedService(driver, source, frame_interval=0.01)
    task = __import__("asyncio").create_task(service.run_forever())
    await __import__("asyncio").sleep(0.05)
    task.cancel()
    with pytest.raises(__import__("asyncio").CancelledError):
        await task
    assert driver.values[-1] == 0
    assert driver.closed is True
