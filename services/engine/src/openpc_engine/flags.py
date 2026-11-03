"""Flag files the LED service reads.

``/run/openpc/engine-busy`` exists exactly while an agent turn is in flight. The
LED owns the interpretation; the engine only maintains the file, which keeps the
two services from importing each other and survives either one restarting.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("openpc.flags")


class ActivityFlag:
    def __init__(self, run_dir: Path) -> None:
        self.path = Path(run_dir) / "engine-busy"
        self._present = False

    def set_busy(self, busy: bool) -> None:
        if busy == self._present:
            return
        try:
            if busy:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text("working\n")
            else:
                self.path.unlink(missing_ok=True)
            self._present = busy
        except OSError as exc:
            # A read-only /run or a missing directory must never break a turn.
            log.debug("could not update activity flag", extra={"detail": str(exc)})

    def clear(self) -> None:
        self.set_busy(False)


class AttentionFlag:
    """Raised by any service that needs a human; cleared by an operator."""

    def __init__(self, run_dir: Path) -> None:
        self.path = Path(run_dir) / "attention"

    def raise_flag(self, reason: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(reason.strip()[:500] + "\n")
        except OSError as exc:
            log.warning("could not raise attention flag", extra={"detail": str(exc)})

    def clear(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not clear attention flag", extra={"detail": str(exc)})

    @property
    def is_raised(self) -> bool:
        return self.path.exists()
