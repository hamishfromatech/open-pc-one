"""Where the LED gets its facts.

Three inputs, deliberately decoupled so no service has to import another:

* **Network presence** - a default IPv4 route exists. Polled (slowly): spawning
  ``ip`` at frame rate would be absurd.
* **Engine busy** - ``/run/openpc/engine-busy`` exists. The engine touches it on
  ``agent_start`` and removes it on ``agent_end``.
* **Attention** - ``/run/openpc/attention`` exists. Any service can raise it; the
  file's content is shown in the journal by whoever wrote it.

File flags rather than a socket: they survive a restart of either side, are
readable with ``ls`` over SSH, and cannot deadlock.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .patterns import Signals

log = logging.getLogger("openpc.led.sources")

DEFAULT_RUN_DIR = Path("/run/openpc")


class CommandRunner:
    async def __call__(self, *argv: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode("utf-8", errors="replace")


class FileSignalSource:
    """Reads the flags plus the routing table."""

    def __init__(
        self,
        run_dir: Path = DEFAULT_RUN_DIR,
        runner: CommandRunner | None = None,
        cache_seconds: float = 2.0,
        clock=time.monotonic,
        boot_timeout: float = 90.0,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run = runner or CommandRunner()
        self.cache_seconds = cache_seconds
        self.clock = clock
        #: After this many seconds the box is "up" even if the boot-complete
        #: flag never arrived, so a broken unit cannot leave the LED white
        #: forever.
        self.boot_timeout = boot_timeout
        self._started_at = clock()
        self._network: bool | None = None
        self._network_at = -1e9

    @property
    def attention_file(self) -> Path:
        return self.run_dir / "attention"

    @property
    def busy_file(self) -> Path:
        return self.run_dir / "engine-busy"

    @property
    def boot_complete_file(self) -> Path:
        """Touched once ``openpc.target`` is reached (by openpc-bless)."""
        return self.run_dir / "boot-complete"

    def booting(self) -> bool:
        if self.boot_complete_file.exists():
            return False
        return (self.clock() - self._started_at) < self.boot_timeout

    async def read(self) -> Signals:
        return Signals(
            network_present=await self.network_present(),
            engine_busy=self.busy_file.exists(),
            attention=self.attention_file.exists(),
            booting=self.booting(),
        )

    async def network_present(self) -> bool:
        now = self.clock()
        if self._network is not None and now - self._network_at < self.cache_seconds:
            return self._network
        rc, out = await self.run("ip", "-4", "route", "show", "default")
        self._network = rc == 0 and bool(out.strip())
        self._network_at = now
        return self._network

    def invalidate(self) -> None:
        self._network = None
