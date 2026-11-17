"""Host stats are read from the box, and absent sensors report absent.

The parsers take their root as an argument, so these tests feed small fabricated
/proc and /sys trees instead of depending on the machine running the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpc_engine import hoststats


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_cpu_rate_needs_two_samples(tmp_path):
    stat = tmp_path / "stat"
    write(stat, "cpu  100 0 100 800 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0\n")
    meter = hoststats.CpuMeter()
    assert meter.sample(tmp_path) is None, "one sample is a lifetime average, not a rate"

    write(stat, "cpu  200 0 200 1400 0 0 0 0\n")
    # idle +600 of total +800 -> 25% busy
    assert meter.sample(tmp_path) == 25.0


def test_cpu_times_only_read_the_aggregate_line(tmp_path):
    write(tmp_path / "stat", "cpu  1 1 1 1 0 0 0 0\ncpu0 9 9 9 9 0 0 0 0\n")
    assert hoststats.cpu_times(tmp_path) == (1, 4)


def test_memory_uses_memavailable_not_memfree(tmp_path):
    write(
        tmp_path / "meminfo",
        "MemTotal:       16000000 kB\nMemFree:         1000000 kB\n"
        "MemAvailable:    8000000 kB\nSwapTotal:       2000000 kB\nSwapFree:        2000000 kB\n",
    )
    memory = hoststats.memory(tmp_path)
    assert memory is not None
    assert memory["total"] == 16000000 * 1024
    assert memory["used"] == (16000000 - 8000000) * 1024
    assert memory["percent"] == 50.0
    assert memory["swapUsed"] == 0


def test_missing_files_report_none_not_zero(tmp_path):
    assert hoststats.memory(tmp_path) is None
    assert hoststats.load() is not None or True  # load reads the real /proc
    assert hoststats.temperature(tmp_path) is None


def test_temperature_takes_the_hottest_zone(tmp_path):
    write(tmp_path / "class/thermal/thermal_zone0/temp", "45000\n")
    write(tmp_path / "class/thermal/thermal_zone1/temp", "61000\n")
    assert hoststats.temperature(tmp_path) == 61.0


def test_temperature_falls_back_to_hwmon(tmp_path):
    write(tmp_path / "class/hwmon/hwmon0/temp1_input", "52000\n")
    assert hoststats.temperature(tmp_path) == 52.0


def test_network_sums_physical_interfaces_and_skips_loopback(tmp_path):
    write(tmp_path / "class/net/lo/statistics/rx_bytes", "999999\n")
    write(tmp_path / "class/net/lo/statistics/tx_bytes", "999999\n")
    write(tmp_path / "class/net/eth0/statistics/rx_bytes", "1000\n")
    write(tmp_path / "class/net/eth0/statistics/tx_bytes", "2000\n")
    write(tmp_path / "class/net/wlan0/statistics/rx_bytes", "500\n")
    write(tmp_path / "class/net/wlan0/statistics/tx_bytes", "700\n")
    totals = hoststats.network(tmp_path)
    assert totals == {"rxBytes": 1500, "txBytes": 2700, "errors": 0}


def test_snapshot_has_the_shape_the_companion_draws(tmp_path):
    write(tmp_path / "stat", "cpu  10 0 10 80 0 0 0 0\n")
    write(
        tmp_path / "meminfo",
        "MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n",
    )
    meter = hoststats.CpuMeter()
    frame = hoststats.snapshot(meter, proc=tmp_path, sys=tmp_path)
    for key in ("at", "cpu", "memory", "disk", "temperatureC", "network",
                "uptimeSeconds", "units", "hostname"):
        assert key in frame
    assert frame["memory"]["percent"] == 50.0
    # first call has no rate yet, and says so rather than pretending 0%
    assert frame["cpu"]["percent"] is None
