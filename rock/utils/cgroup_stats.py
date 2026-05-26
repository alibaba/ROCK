"""Container-aware CPU/memory metrics via cgroup v1/v2.

Falls back to psutil when cgroup files are unavailable (e.g. running
outside a container or on non-Linux platforms).
"""

import os
import time
from pathlib import Path

import psutil


class CgroupCpuStats:
    """Reads container CPU utilization from cgroup v1/v2 pseudo-files."""

    def __init__(self):
        self._last_cpu_usage: int | None = None
        self._last_cpu_time: int | None = None
        self._cgroup_version: int | None = None
        self._cpu_quota: float | None = None

    def _detect_cgroup_version(self) -> int:
        if self._cgroup_version is not None:
            return self._cgroup_version

        if Path("/sys/fs/cgroup/cgroup.controllers").exists():
            self._cgroup_version = 2
        elif Path("/sys/fs/cgroup/cpu/cpuacct.usage").exists() or Path("/sys/fs/cgroup/cpuacct/cpuacct.usage").exists():
            self._cgroup_version = 1
        else:
            self._cgroup_version = 0

        return self._cgroup_version

    def _read_cpu_usage_ns(self) -> int | None:
        try:
            ver = self._detect_cgroup_version()
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

    def _read_cpu_quota(self) -> float:
        if self._cpu_quota is not None:
            return self._cpu_quota

        try:
            ver = self._detect_cgroup_version()
            if ver == 2:
                text = Path("/sys/fs/cgroup/cpu.max").read_text().strip()
                parts = text.split()
                if parts[0] == "max":
                    self._cpu_quota = float(os.cpu_count() or 1)
                    return self._cpu_quota
                quota = int(parts[0])
                period = int(parts[1])
                if quota <= 0 or period <= 0:
                    self._cpu_quota = float(os.cpu_count() or 1)
                    return self._cpu_quota
                self._cpu_quota = quota / period
                return self._cpu_quota
            elif ver == 1:
                quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text().strip())
                period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip())
                if quota <= 0 or period <= 0:
                    self._cpu_quota = float(os.cpu_count() or 1)
                    return self._cpu_quota
                self._cpu_quota = quota / period
                return self._cpu_quota
        except Exception:
            pass
        self._cpu_quota = float(os.cpu_count() or 1)
        return self._cpu_quota

    def cpu_percent(self) -> float:
        """Return container CPU utilization % since last call.

        First call returns 0.0 (no baseline). Falls back to psutil if cgroup
        files are unavailable.
        """
        usage_ns = self._read_cpu_usage_ns()
        now_ns = time.monotonic_ns()

        if usage_ns is None:
            return psutil.cpu_percent()

        prev_usage = self._last_cpu_usage
        prev_time = self._last_cpu_time
        self._last_cpu_usage = usage_ns
        self._last_cpu_time = now_ns

        if prev_usage is None or prev_time is None:
            return 0.0

        delta_usage = usage_ns - prev_usage
        delta_time = now_ns - prev_time
        if delta_time <= 0 or delta_usage < 0:
            return 0.0

        num_cpus = self._read_cpu_quota()
        return min(round((delta_usage / delta_time) / num_cpus * 100, 1), 100.0)


class CgroupMemStats:
    """Reads container memory utilization from cgroup v1/v2 pseudo-files."""

    def __init__(self):
        self._cgroup_version: int | None = None
        self._mem_limit: int | None = None

    def _detect_cgroup_version(self) -> int:
        if self._cgroup_version is not None:
            return self._cgroup_version

        if Path("/sys/fs/cgroup/cgroup.controllers").exists():
            self._cgroup_version = 2
        elif Path("/sys/fs/cgroup/memory/memory.usage_in_bytes").exists():
            self._cgroup_version = 1
        else:
            self._cgroup_version = 0

        return self._cgroup_version

    def _read_mem_stat(self, stat_path: str, key: str) -> int:
        """Read a single counter from a cgroup memory.stat file. Returns 0 on any error."""
        try:
            for line in Path(stat_path).read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == key:
                    return int(parts[1])
        except Exception:
            pass
        return 0

    def _read_mem_usage_bytes(self) -> int | None:
        """Return container memory usage minus reclaimable page cache (inactive_file).

        Raw cgroup usage counters include page cache, which is reclaimable and not
        a meaningful signal of application memory pressure. Subtracting inactive_file
        matches the working-set definition used by docker stats and kubelet.
        """
        try:
            ver = self._detect_cgroup_version()
            if ver == 2:
                usage = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
                inactive_file = self._read_mem_stat("/sys/fs/cgroup/memory.stat", "inactive_file")
                return max(usage - inactive_file, 0)
            elif ver == 1:
                usage = int(Path("/sys/fs/cgroup/memory/memory.usage_in_bytes").read_text().strip())
                inactive_file = self._read_mem_stat("/sys/fs/cgroup/memory/memory.stat", "total_inactive_file")
                return max(usage - inactive_file, 0)
            return None
        except Exception:
            return None

    def _read_mem_limit_bytes(self) -> int | None:
        if self._mem_limit is not None:
            return self._mem_limit

        try:
            ver = self._detect_cgroup_version()
            if ver == 2:
                text = Path("/sys/fs/cgroup/memory.max").read_text().strip()
                if text == "max":
                    return None
                limit = int(text)
                if limit <= 0:
                    return None
                self._mem_limit = limit
                return self._mem_limit
            elif ver == 1:
                limit = int(Path("/sys/fs/cgroup/memory/memory.limit_in_bytes").read_text().strip())
                # cgroup v1 uses a very large number (like PAGE_COUNTER_MAX) for unlimited
                if limit <= 0 or limit >= (1 << 62):
                    return None
                self._mem_limit = limit
                return self._mem_limit
        except Exception:
            pass
        return None

    def mem_percent(self) -> float:
        """Return container memory utilization %.

        Falls back to psutil if cgroup files are unavailable or memory is unlimited.
        """
        usage = self._read_mem_usage_bytes()
        limit = self._read_mem_limit_bytes()

        if usage is None or limit is None:
            return psutil.virtual_memory().percent

        return min(round(usage / limit * 100, 1), 100.0)
