"""Container-aware CPU metrics via cgroup v1/v2.

Falls back to psutil when cgroup files are unavailable (e.g. running
outside a container or on non-Linux platforms).
"""

import os
import time
from pathlib import Path

import psutil

_last_cpu_usage: int | None = None
_last_cpu_time: int | None = None

_cgroup_version: int | None = None
_cpu_quota: float | None = None


def _detect_cgroup_version() -> int:
    global _cgroup_version
    if _cgroup_version is not None:
        return _cgroup_version

    if Path("/sys/fs/cgroup/cgroup.controllers").exists():
        _cgroup_version = 2
    elif Path("/sys/fs/cgroup/cpu/cpuacct.usage").exists() or Path("/sys/fs/cgroup/cpuacct/cpuacct.usage").exists():
        _cgroup_version = 1
    else:
        _cgroup_version = 0

    return _cgroup_version


def _read_cpu_usage_ns() -> int | None:
    try:
        ver = _detect_cgroup_version()
        if ver == 2:
            text = Path("/sys/fs/cgroup/cpu.stat").read_text()
            for line in text.splitlines():
                if line.startswith("usage_usec"):
                    return int(line.split()[1]) * 1000
            return None
        elif ver == 1:
            for path in ("/sys/fs/cgroup/cpu/cpuacct.usage", "/sys/fs/cgroup/cpuacct/cpuacct.usage"):
                p = Path(path)
                if p.exists():
                    return int(p.read_text().strip())
            return None
        return None
    except Exception:
        return None


def _read_cpu_quota() -> float:
    global _cpu_quota
    if _cpu_quota is not None:
        return _cpu_quota

    try:
        ver = _detect_cgroup_version()
        if ver == 2:
            text = Path("/sys/fs/cgroup/cpu.max").read_text().strip()
            parts = text.split()
            if parts[0] == "max":
                _cpu_quota = float(os.cpu_count() or 1)
                return _cpu_quota
            quota = int(parts[0])
            period = int(parts[1])
            if quota <= 0 or period <= 0:
                _cpu_quota = float(os.cpu_count() or 1)
                return _cpu_quota
            _cpu_quota = quota / period
            return _cpu_quota
        elif ver == 1:
            quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text().strip())
            period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip())
            if quota <= 0 or period <= 0:
                _cpu_quota = float(os.cpu_count() or 1)
                return _cpu_quota
            _cpu_quota = quota / period
            return _cpu_quota
    except Exception:
        pass
    _cpu_quota = float(os.cpu_count() or 1)
    return _cpu_quota


def cpu_percent() -> float:
    """Return container CPU utilization % since last call.

    First call returns 0.0 (no baseline). Falls back to psutil if cgroup
    files are unavailable.
    """
    global _last_cpu_usage, _last_cpu_time

    usage_ns = _read_cpu_usage_ns()
    now_ns = time.monotonic_ns()

    if usage_ns is None:
        return psutil.cpu_percent()

    prev_usage = _last_cpu_usage
    prev_time = _last_cpu_time
    _last_cpu_usage = usage_ns
    _last_cpu_time = now_ns

    if prev_usage is None or prev_time is None:
        return 0.0

    delta_usage = usage_ns - prev_usage
    delta_time = now_ns - prev_time
    if delta_time <= 0 or delta_usage < 0:
        return 0.0

    num_cpus = _read_cpu_quota()
    return min(round((delta_usage / delta_time) / num_cpus * 100, 1), 100.0)
