"""What the customer can ask for, as data rather than as code.

Every entry is a *profile*: a named bundle of things to enable on this box. The
setup UI renders this list, so adding a profile is a data change and a support
question ("what does 'Local AI' install?") has one honest answer.

Two kinds of action, on purpose:

* ``units``  — systemd units already in the image. Enabling them is offline-safe,
              instant and reversible, so this is the default for everything that
              ships in the image.
* ``packages`` — Debian packages installed at setup time. That needs the network
              and a working mirror, so a profile that uses them says so and the
              apply step checks connectivity before promising anything.

``requires`` lets the UI disable a choice honestly instead of failing halfway
through an install (see "voice": the speech image does not exist yet).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    name: str
    summary: str
    #: Not offered as a choice; always applied.
    always: bool = False
    default: bool = False
    units: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    #: Shown when the profile cannot be chosen yet, and why.
    unavailable: str = ""

    @property
    def available(self) -> bool:
        return not self.unavailable

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "always": self.always,
            "default": self.default,
            "units": list(self.units),
            "packages": list(self.packages),
            "requiresNetwork": bool(self.packages),
            "available": self.available,
            "unavailable": self.unavailable,
        }


CATALOG: tuple[Profile, ...] = (
    Profile(
        id="agent",
        name="Agent runtime",
        summary="The part that makes this a computer an agent can drive: the WSS "
                "engine, the desktop session and semantic control.",
        always=True,
        units=("openpc-engine.service", "openpc-session.service", "cua-driver.service"),
    ),
    Profile(
        id="local-ai",
        name="Local AI",
        summary="Run a model on the box itself, offline. Nothing leaves the device.",
        default=True,
        units=("llama.service",),
    ),
    Profile(
        id="voice",
        name="Voice",
        summary="Speech in and out, so you can talk to it across the room.",
        requires=("speech-image",),
        unavailable="The speech image is not built yet; enabling this would "
                    "start a unit that has nothing to run.",
    ),
    Profile(
        id="dev-tools",
        name="Developer tools",
        summary="Git, a compiler and Python packaging, for when the agent writes and "
                "runs its own code.",
        packages=("git", "build-essential", "python3-pip"),
    ),
    Profile(
        id="browser-extras",
        name="Media support",
        summary="Codecs, fonts and PDF tools so the built-in browser is not crippled.",
        packages=("ffmpeg", "fonts-noto-core", "poppler-utils"),
    ),
)


def by_id(profile_id: str) -> Profile:
    for profile in CATALOG:
        if profile.id == profile_id:
            return profile
    raise KeyError(profile_id)


def selectable() -> list[Profile]:
    return [p for p in CATALOG if not p.always]


def defaults() -> list[str]:
    return [p.id for p in CATALOG if p.always or p.default]


def resolve(chosen: list[str]) -> list[Profile]:
    """Expand a selection into the profiles that will actually be applied.

    Unknown ids are an error (a client bug), unavailable ones are an error with a
    reason (a UI bug), and ``always`` profiles are added whether or not the
    customer ticked them. Order follows the catalog, so the install log reads the
    same way every time.
    """
    wanted: set[str] = set()
    for profile_id in chosen:
        try:
            profile = by_id(profile_id)
        except KeyError as exc:
            raise ValueError(f"unknown profile {profile_id!r}") from exc
        if not profile.available:
            raise ValueError(f"{profile.name}: {profile.unavailable}")
        wanted.add(profile.id)
    wanted.update(p.id for p in CATALOG if p.always)
    return [p for p in CATALOG if p.id in wanted]
