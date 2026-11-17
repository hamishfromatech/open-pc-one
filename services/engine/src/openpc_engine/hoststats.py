"""Host resource statistics, read from /proc and /sys.

The companion app shows the customer what the box is doing, and the box is *theirs*
— so these numbers are the ones on the appliance, not the agent's usage. Everything
here degrades to ``None`` rather than zero: a container without a thermal zone is
not 0 °C, and a UI that prints 0 °C is lying about a sensor it does not have.

Read paths are injectable so the parsers can be tested without the host's numbers.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Protocol

PROC = Path("/proc")
SYS = Path("/sys")


class DiskUsage(Protocol):  # pragma: no cover - typing only
    total: int
    used: int
    free: int


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def cpu_times(proc: Path = PROC) -> tuple[int, int]:
    """(idle, total) jiffies from the first /proc/stat line.

    The first line is the aggregate; per-core lines follow and are deliberately
    ignored, because a 4-core box reporting 4 CPUs' worth of totals is how a CPU
    meter ends up at 400%.
    """
    line = _read(proc / "stat").splitlines()[0] if _read(proc / "stat") else ""
    if not line.startswith("cpu"):
        return (0, 0)
    fields = [int(value) for value in line.split()[1:] if value.isdigit()]
    if len(fields) < 4:
        return (0, 0)
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
    return (idle, sum(fields[:8]))


class CpuMeter:
    """CPU usage needs two samples; one sample is a lifetime average.

    The previous sample is kept, so a caller polling once a second gets a real
    figure instead of the box's average since boot.
    """

    def __init__(self) -> None:
        self._last: tuple[int, int] | None = None

    def sample(self, proc: Path = PROC) -> float | None:
        idle, total = cpu_times(proc)
        if total == 0:
            return None
        previous = self._last
        self._last = (idle, total)
        if previous is None:
            return None
        idle_delta = idle - previous[0]
        total_delta = total - previous[1]
        if total_delta <= 0:
            return None
        return round(100.0 * (1.0 - idle_delta / total_delta), 1)


def memory(proc: Path = PROC) -> dict[str, int] | None:
    """/proc/meminfo in bytes. ``MemAvailable`` is the honest headroom number."""
    text = _read(proc / "meminfo")
    if not text:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024
    if "MemTotal" not in values:
        return None
    total = values["MemTotal"]
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return {
        "total": total,
        "available": available,
        "used": max(0, total - available),
        "percent": round(100.0 * (total - available) / total, 1) if total else 0.0,
        "swapTotal": values.get("SwapTotal", 0),
        "swapUsed": max(0, values.get("SwapTotal", 0) - values.get("SwapFree", 0)),
    }


def load() -> dict[str, float] | None:
    text = _read(PROC / "loadavg")
    if not text:
        return None
    parts = text.split()
    try:
        return {"1m": float(parts[0]), "5m": float(parts[1]), "15m": float(parts[2])}
    except (IndexError, ValueError):
        return None


def uptime() -> float | None:
    text = _read(PROC / "uptime")
    try:
        return round(float(text.split()[0]), 1)
    except (IndexError, ValueError):
        return None


def disk(path: str = "/") -> dict[str, int] | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0,
    }


def temperature(sys: Path = SYS) -> float | None:
    """Hottest thermal zone in °C.

    A headless box with no thermal zone (most VMs, most containers) returns None:
    an appliance whose fan curve is hidden is worse than one that admits it.
    """
    readings: list[float] = []
    base = sys / "class" / "thermal"
    if base.is_dir():
        for zone in sorted(base.glob("thermal_zone*")):
            value = _read(zone / "temp")
            if value.lstrip("-").isdigit():
                readings.append(int(value) / 1000.0)
    if readings:
        return round(max(readings), 1)
    for hwmon in sorted((sys / "class" / "hwmon").glob("hwmon*")):
        for sensor in sorted(hwmon.glob("temp*_input")):
            value = _read(sensor)
            if value.lstrip("-").isdigit():
                readings.append(int(value) / 1000.0)
    return round(max(readings), 1) if readings else None


def network(sys: Path = SYS) -> dict[str, int] | None:
    """Cumulative bytes and errors across physical interfaces (lo excluded)."""
    received = sent = errors = 0
    seen = False
    base = sys / "class" / "net"
    if not base.is_dir():
        return None
    for interface in sorted(base.iterdir()):
        if interface.name == "lo":
            continue
        rx = _read(interface / "statistics" / "rx_bytes")
        tx = _read(interface / "statistics" / "tx_bytes")
        if not (rx.isdigit() and tx.isdigit()):
            continue
        seen = True
        received += int(rx)
        sent += int(tx)
        for counter in ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
            value = _read(interface / "statistics" / counter)
            if value.isdigit():
                errors += int(value)
    if not seen:
        return None
    return {"rxBytes": received, "txBytes": sent, "errors": errors}


def running_units() -> dict[str, str] | None:
    """Which of our units are up, when systemd is there to ask."""
    if shutil.which("systemctl") is None:
        return None
    import subprocess

    states: dict[str, str] = {}
    for unit in ("openpc-engine", "openpc-session", "llama", "cua-driver", "openpc-led"):
        try:
            done = subprocess.run(
                ["systemctl", "is-active", f"{unit}.service"],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        states[unit] = (done.stdout or "").strip() or "unknown"
    return states


def snapshot(meter: CpuMeter, *, proc: Path = PROC, sys: Path = SYS) -> dict[str, Any]:
    """Everything the companion app draws, in one flat frame."""
    return {
        "at": time.time(),
        "cpu": {"percent": meter.sample(proc), "load": load()},
        "memory": memory(proc),
        "disk": disk(),
        "temperatureC": temperature(sys),
        "network": network(sys),
        "uptimeSeconds": uptime(),
        "units": running_units(),
        "hostname": os.uname().nodename,
    }
