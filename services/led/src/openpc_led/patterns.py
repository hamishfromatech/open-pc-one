"""The four LED states, as pure functions of time.

The LED is the appliance's only local UI, so its meaning has to be unambiguous
from across a room:

======================  ===============================================
``NO_NETWORK``          amber, solid           - cannot be reached
``IDLE``                blue, slow breathe     - healthy, waiting
``WORKING``             cyan, fast pulse       - agent has a turn in flight
``ATTENTION``           red, double-blink      - needs a human
``BOOTING``             dim white, slow pulse - not up yet
======================  ===============================================

Priority is ``BOOTING > ATTENTION > NO_NETWORK > WORKING > IDLE``. Booting wins
because nothing else is knowable yet (APP-ARCHITECTURE.md §3). Attention is next:
it is the only state that cannot be resolved from a client. No-network outranks
working because a box nobody can reach needs a cable or a phone, not patience.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

MAX_BRIGHTNESS = 255

#: The power button ring is a single-colour LED on the reference unit; the driver
#: maps these to whichever channel the hardware exposes.
COLOURS = {
    "booting": (72, 72, 72),  # dim white
    "no_network": (255, 150, 0),  # amber
    "idle": (0, 96, 255),  # blue
    "working": (0, 224, 255),  # cyan
    "attention": (255, 0, 0),  # red
}


class LedState(str, Enum):
    BOOTING = "booting"
    ATTENTION = "attention"
    NO_NETWORK = "no_network"
    WORKING = "working"
    IDLE = "idle"


#: Lower value = higher priority.
PRIORITY = {
    LedState.BOOTING: 0,
    LedState.ATTENTION: 1,
    LedState.NO_NETWORK: 2,
    LedState.WORKING: 3,
    LedState.IDLE: 4,
}


@dataclass(slots=True)
class Signals:
    """What the LED knows about the box."""

    network_present: bool = True
    engine_busy: bool = False
    attention: bool = False
    booting: bool = False


def resolve_state(signals: Signals) -> LedState:
    if signals.booting:
        return LedState.BOOTING
    if signals.attention:
        return LedState.ATTENTION
    if not signals.network_present:
        return LedState.NO_NETWORK
    if signals.engine_busy:
        return LedState.WORKING
    return LedState.IDLE


def colour(state: LedState) -> tuple[int, int, int]:
    return COLOURS[state.value]


def brightness(state: LedState, t: float) -> int:
    """Brightness at ``t`` seconds on an arbitrary monotonic clock.

    Pure and periodic, so the caller needs no animation loop state and a test can
    assert exact values.
    """
    if state is LedState.NO_NETWORK:
        return MAX_BRIGHTNESS

    if state is LedState.BOOTING:
        # Slow, dim, distinct from the idle breathe's full brightness: "not up
        # yet" must never be mistaken for "idle and healthy".
        period = 3.0
        phase = (t % period) / period
        wave = (1.0 - math.cos(2.0 * math.pi * phase)) / 2.0
        return int(round(24 + 156 * wave))

    if state is LedState.IDLE:
        # Slow breathe: 2.4 s cycle, raised cosine, never fully off - a dark ring
        # reads as "dead box".
        period = 2.4
        phase = (t % period) / period
        wave = (1.0 - math.cos(2.0 * math.pi * phase)) / 2.0
        return int(round(18 + (MAX_BRIGHTNESS - 18) * wave))

    if state is LedState.WORKING:
        # Fast pulse: 4 Hz square. Deliberately mechanical - "working" should
        # never be mistaken for the gentle idle breathe.
        period = 0.25
        return MAX_BRIGHTNESS if (t % period) < period / 2 else 0

    # ATTENTION: two 120 ms blinks 280 ms apart, then dark for 1.6 s.
    period = 2.0
    phase = t % period
    if phase < 0.12 or 0.28 <= phase < 0.40:
        return MAX_BRIGHTNESS
    return 0


def is_lit(state: LedState, t: float) -> bool:
    return brightness(state, t) > 0
