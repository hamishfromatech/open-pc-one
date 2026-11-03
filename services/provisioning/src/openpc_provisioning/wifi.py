"""NetworkManager joins, without ever putting a passphrase on a command line.

``nmcli ... password <secret>`` puts the PSK in ``/proc/<pid>/cmdline`` and in
shell history. Instead this module writes a NetworkManager keyfile (mode 0600,
root-owned) and asks NetworkManager to bring it up. NetworkManager stores the
PSK in its own root-only store, so the plaintext file is removed after import.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .state import WifiCredentials

log = logging.getLogger("openpc.provisioning.wifi")

DEFAULT_KEYFILE_DIR = Path("/etc/NetworkManager/system-connections")
PROFILE_ID = "openpc-wifi"
#: Fixed namespace for profile UUIDs so re-provisioning overwrites in place
#: rather than piling up profiles on a read-only root.
_UUID_NS = uuid.UUID("5f3a0001-6f70-6e63-0000-6f70656e7063")

_IP = re.compile(r"inet\s+(\d+\.\d+\.\d+\.\d+)")


@dataclass(slots=True)
class JoinResult:
    ok: bool
    ip: str | None = None
    error: str | None = None


class CommandRunner(Protocol):
    async def __call__(self, *argv: str) -> tuple[int, str]: ...


class SubprocessRunner:
    """Run a command with no shell and no inherited secrets."""

    async def __call__(self, *argv: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode("utf-8", errors="replace")


class WifiBackend(Protocol):
    async def is_online(self) -> bool: ...

    async def current_ip(self) -> str | None: ...

    async def join(self, credentials: WifiCredentials) -> JoinResult: ...


def _escape_keyfile(value: str) -> str:
    """Escape a keyfile value.

    Newlines and other control characters are rejected upstream, so the only
    transform needed is a backslash escape, which keeps a literal ``\\`` in an
    SSID from being read as an escape introducer.
    """
    return value.replace("\\", "\\\\")


def render_keyfile(credentials: WifiCredentials, *, interface: str = "wlan0", hidden: bool = False) -> str:
    profile_uuid = str(uuid.uuid5(_UUID_NS, PROFILE_ID))
    security = ""
    if not credentials.is_open:
        security = (
            "\n[wifi-security]\n"
            "key-mgmt=wpa-psk\n"
            f"psk={_escape_keyfile(credentials.password)}\n"
        )
    return (
        "[connection]\n"
        f"id={PROFILE_ID}\n"
        f"uuid={profile_uuid}\n"
        "type=wifi\n"
        f"interface-name={interface}\n"
        "autoconnect=true\n"
        "autoconnect-priority=10\n"
        "\n[wifi]\n"
        "mode=infrastructure\n"
        f"ssid={_escape_keyfile(credentials.ssid)}\n"
        f"hidden={'true' if (hidden or credentials.hidden) else 'false'}\n"
        f"{security}"
        "\n[ipv4]\n"
        "method=auto\n"
        "\n[ipv6]\n"
        "method=auto\n"
    )


class NmcliBackend:
    """NetworkManager backend used on the appliance."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        interface: str = "wlan0",
        keyfile_dir: Path = DEFAULT_KEYFILE_DIR,
        timeout: float = 45.0,
        poll_interval: float = 1.0,
    ) -> None:
        self.run = runner or SubprocessRunner()
        self.interface = interface
        self.keyfile_dir = Path(keyfile_dir)
        self.timeout = timeout
        self.poll_interval = poll_interval

    @property
    def keyfile(self) -> Path:
        return self.keyfile_dir / f"{PROFILE_ID}.nmconnection"

    # -- state -------------------------------------------------------------

    async def is_online(self) -> bool:
        rc, out = await self.run("ip", "-4", "route", "show", "default")
        return rc == 0 and bool(out.strip())

    async def current_ip(self) -> str | None:
        rc, out = await self.run("ip", "-4", "-o", "addr", "show", self.interface)
        if rc != 0:
            return None
        match = _IP.search(out)
        return match.group(1) if match else None

    # -- join --------------------------------------------------------------

    async def join(self, credentials: WifiCredentials) -> JoinResult:
        try:
            self._write_keyfile(credentials)
        except OSError as exc:
            return JoinResult(ok=False, error=f"cannot write network profile: {exc.strerror or exc}")

        rc, out = await self.run("nmcli", "connection", "reload")
        if rc != 0:
            return JoinResult(ok=False, error=f"nmcli reload failed: {out.strip()[:200]}")

        rc, out = await self.run("nmcli", "connection", "up", PROFILE_ID)
        if rc != 0:
            # NetworkManager reports the failure reason (wrong password, SSID
            # absent). It is safe to surface: it contains no secret.
            return JoinResult(ok=False, error=_summarise(out))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while loop.time() < deadline:
            if await self.is_online():
                return JoinResult(ok=True, ip=await self.current_ip())
            await asyncio.sleep(self.poll_interval)
        return JoinResult(ok=False, error="timed out waiting for a DHCP lease")

    def _write_keyfile(self, credentials: WifiCredentials) -> None:
        self.keyfile_dir.mkdir(parents=True, exist_ok=True)
        payload = render_keyfile(credentials, interface=self.interface)
        tmp = self.keyfile.with_suffix(".nmconnection.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)  # in case the umask applied to O_CREAT
        os.replace(tmp, self.keyfile)
        log.info("wrote network profile", extra={"detail": f"ssid={credentials.ssid!r}"})


def _summarise(output: str) -> str:
    """Reduce nmcli's multi-line error to one safe sentence."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line and "Error:" in line:
            return line.split("Error:", 1)[1].strip()[:200]
    return (output.strip().splitlines() or ["connection failed"])[-1][:200]
