"""Setup state: the two-phase handoff between the factory and the customer.

One JSON file is the source of truth, so a power cut at any point is recoverable
and the OEM can ship a box that has been *checked* without being *claimed*:

    welcome     the first screen. The OEM sees it, proves the image boots and
                powers off; the customer later sees the same screen.
    licensed    a license key verified. Nothing is installed yet.
    installing  the chosen profiles are being applied.
    complete    the desktop replaces the setup screen on the next boot.

``factory_checks`` counts how many times the welcome screen was completed and the
box powered off without a key — that is the OEM's "yes, this unit works" mark,
and it deliberately does not consume anything the customer needs.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PHASES = ("welcome", "licensed", "installing", "complete")

#: Only these moves are legal. Anything else is a bug in the caller, not a
#: recoverable condition, so it raises instead of being silently ignored.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "welcome": ("licensed",),
    "licensed": ("installing", "welcome"),
    "installing": ("complete", "licensed"),
    "complete": ("installing",),
}


class StateError(RuntimeError):
    """The requested phase change is not legal from the current phase."""


@dataclass(slots=True)
class SetupState:
    phase: str = "welcome"
    #: Written once at first boot; the OEM's serial or our batch id.
    device_id: str = ""
    batch: str = ""
    factory_checks: int = 0
    #: Set when a license verified. The key itself is never stored.
    license_id: str = ""
    license_tier: str = ""
    license_batch: str = ""
    profiles: list[str] = field(default_factory=list)
    installed: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> SetupState:
        if not path.exists():
            state = cls(device_id=f"OPC-{secrets.token_hex(4).upper()}")
            state.save(path)
            return state
        raw: dict[str, Any] = json.loads(path.read_text())
        known = {f for f in cls.__dataclass_fields__}  # forward/backward tolerant
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path) -> None:
        self.updated_at = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(asdict(self), indent=2).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    # -- transitions -------------------------------------------------------

    def move(self, phase: str) -> None:
        if phase not in PHASES:
            raise StateError(f"unknown phase {phase!r}")
        if phase not in TRANSITIONS[self.phase]:
            raise StateError(f"cannot go from {self.phase} to {phase}")
        self.phase = phase

    def note_factory_check(self) -> None:
        """The OEM reached the welcome screen and powered off to ship."""
        self.factory_checks += 1

    @property
    def ready_for_customer(self) -> bool:
        return self.phase == "welcome"

    @property
    def complete(self) -> bool:
        return self.phase == "complete"

    def public(self) -> dict[str, Any]:
        """What the local UI may see. No secrets live here, but the shape is
        deliberately explicit so nothing leaks by accident if it ever does."""
        return {
            "phase": self.phase,
            "deviceId": self.device_id,
            "batch": self.batch,
            "factoryChecks": self.factory_checks,
            "licenseId": self.license_id,
            "licenseTier": self.license_tier,
            "profiles": list(self.profiles),
            "installed": list(self.installed),
            "updatedAt": self.updated_at,
        }
