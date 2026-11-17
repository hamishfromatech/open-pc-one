"""Turn chosen profiles into a working box, and say what happened.

The apply step is the only part of setup that changes the system, and it is
written to be safe to run twice: ``systemctl enable --now`` is idempotent, and a
package that is already installed is skipped. It records each step so the UI can
show progress instead of a spinner with no end, and so a failed install can be
reported as "this one line failed" rather than "setup failed".
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field

from .catalog import Profile


@dataclass(slots=True)
class Step:
    profile: str
    kind: str  # "unit" | "package"
    target: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0

    def public(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "kind": self.kind,
            "target": self.target,
            "ok": self.ok,
            "detail": self.detail,
            "seconds": round(self.seconds, 2),
        }


@dataclass(slots=True)
class Result:
    steps: list[Step] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    def public(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "dryRun": self.dry_run,
            "steps": [s.public() for s in self.steps],
        }


def online() -> bool:
    """A package install needs a route out; a units-only profile does not."""
    if shutil.which("systemctl") is None:  # container / dev path
        return False
    try:
        completed = subprocess.run(
            ["systemctl", "is-system-running"], capture_output=True, timeout=5, text=True
        )
        # "degraded" still means systemd is up; the network check is separate.
        return completed.returncode in (0, 1)
    except (OSError, subprocess.SubprocessError):
        return False


def has_network() -> bool:
    if shutil.which("ping") is None:
        return False
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", "2", "deb.debian.org"],
            capture_output=True,
            timeout=5,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run(argv: list[str], dry_run: bool) -> tuple[bool, str]:
    if dry_run:
        return True, "dry run: " + " ".join(argv)
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    detail = (completed.stdout or completed.stderr or "").strip().splitlines()
    tail = detail[-1] if detail else ""
    return completed.returncode == 0, tail[:400]


def apply_profiles(profiles: list[Profile], *, dry_run: bool = False) -> Result:
    """Apply every action of every profile, in catalog order.

    A failed package install does not abort the run: the units that do not need
    the network still come up, and the customer gets a box that works with a
    short, specific list of things that did not.
    """
    result = Result(dry_run=dry_run)
    can_reach_network: bool | None = None

    for profile in profiles:
        for unit in profile.units:
            started = time.monotonic()
            ok, detail = _run(["systemctl", "enable", "--now", unit], dry_run)
            result.steps.append(
                Step(profile.id, "unit", unit, ok, detail, time.monotonic() - started)
            )

        if not profile.packages:
            continue

        if can_reach_network is None:
            can_reach_network = online() and has_network()
        if not can_reach_network:
            result.steps.append(
                Step(profile.id, "package", ", ".join(profile.packages), False,
                     "no network: skipped, run setup again when the box is online")
            )
            continue

        started = time.monotonic()
        ok, detail = _run(
            ["apt-get", "install", "-y", "--no-install-recommends", *profile.packages], dry_run
        )
        result.steps.append(
            Step(profile.id, "package", ", ".join(profile.packages), ok, detail,
                 time.monotonic() - started)
        )

    return result
