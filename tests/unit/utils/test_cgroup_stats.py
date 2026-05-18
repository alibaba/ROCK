"""Tests for rock.utils.cgroup_stats — container-aware CPU metrics."""

from unittest.mock import patch

import pytest

from rock.utils.cgroup_stats import CgroupCpuStats, Path


def _mock_path_exists(mapping: dict[str, bool]):
    """Return a side_effect for Path.exists() based on a path-string mapping."""
    original_exists = Path.exists

    def _exists(self):
        s = str(self)
        if s in mapping:
            return mapping[s]
        return original_exists(self)

    return _exists


def _mock_path_read_text(mapping: dict[str, str]):
    """Return a side_effect for Path.read_text() based on a path-string mapping."""

    def _read_text(self, *args, **kwargs):
        s = str(self)
        if s in mapping:
            return mapping[s]
        raise FileNotFoundError(s)

    return _read_text


# ---------- cgroup version detection ----------


class TestDetectCgroupVersion:
    def test_detects_v2(self):
        stats = CgroupCpuStats()
        exists_map = {"/sys/fs/cgroup/cgroup.controllers": True}
        with patch.object(Path, "exists", _mock_path_exists(exists_map)):
            assert stats._detect_cgroup_version() == 2

    def test_detects_v1(self):
        stats = CgroupCpuStats()
        exists_map = {
            "/sys/fs/cgroup/cgroup.controllers": False,
            "/sys/fs/cgroup/cpu/cpuacct.usage": True,
        }
        with patch.object(Path, "exists", _mock_path_exists(exists_map)):
            assert stats._detect_cgroup_version() == 1

    def test_detects_v1_alternate_path(self):
        stats = CgroupCpuStats()
        exists_map = {
            "/sys/fs/cgroup/cgroup.controllers": False,
            "/sys/fs/cgroup/cpu/cpuacct.usage": False,
            "/sys/fs/cgroup/cpuacct/cpuacct.usage": True,
        }
        with patch.object(Path, "exists", _mock_path_exists(exists_map)):
            assert stats._detect_cgroup_version() == 1

    def test_detects_no_cgroup(self):
        stats = CgroupCpuStats()
        exists_map = {
            "/sys/fs/cgroup/cgroup.controllers": False,
            "/sys/fs/cgroup/cpu/cpuacct.usage": False,
            "/sys/fs/cgroup/cpuacct/cpuacct.usage": False,
        }
        with patch.object(Path, "exists", _mock_path_exists(exists_map)):
            assert stats._detect_cgroup_version() == 0

    def test_caches_result(self):
        stats = CgroupCpuStats()
        stats._cgroup_version = 2
        assert stats._detect_cgroup_version() == 2


# ---------- CPU percent ----------


class TestCpuPercent:
    def test_first_call_returns_zero(self):
        stats = CgroupCpuStats()
        stats._cgroup_version = 2
        read_map = {"/sys/fs/cgroup/cpu.stat": "usage_usec 1000000\n"}
        with patch.object(Path, "read_text", _mock_path_read_text(read_map)):
            assert stats.cpu_percent() == 0.0

    def test_v2_cpu_calculation(self):
        stats = CgroupCpuStats()
        stats._cgroup_version = 2
        read_map_1 = {
            "/sys/fs/cgroup/cpu.stat": "usage_usec 1000000\n",
            "/sys/fs/cgroup/cpu.max": "100000 100000\n",
        }
        read_map_2 = {
            "/sys/fs/cgroup/cpu.stat": "usage_usec 1500000\n",
            "/sys/fs/cgroup/cpu.max": "100000 100000\n",
        }
        time_values = [100_000_000_000, 200_000_000_000]
        time_idx = {"i": 0}

        def mock_monotonic_ns():
            val = time_values[time_idx["i"]]
            time_idx["i"] += 1
            return val

        with (
            patch.object(Path, "read_text", _mock_path_read_text(read_map_1)),
            patch("rock.utils.cgroup_stats.time.monotonic_ns", mock_monotonic_ns),
        ):
            stats.cpu_percent()

        with (
            patch.object(Path, "read_text", _mock_path_read_text(read_map_2)),
            patch("rock.utils.cgroup_stats.time.monotonic_ns", mock_monotonic_ns),
        ):
            result = stats.cpu_percent()

        # delta_usage = (1500000 - 1000000) * 1000 = 500_000_000 ns
        # delta_time = 100_000_000_000 ns
        # quota = 100000/100000 = 1.0 CPU
        # pct = (500_000_000 / 100_000_000_000) / 1.0 * 100 = 0.5%
        assert result == 0.5

    def test_v1_cpu_calculation(self):
        stats = CgroupCpuStats()
        stats._cgroup_version = 1
        exists_map = {"/sys/fs/cgroup/cpu/cpuacct.usage": True}
        read_map_1 = {
            "/sys/fs/cgroup/cpu/cpuacct.usage": "1000000000\n",
            "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "200000\n",
            "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000\n",
        }
        read_map_2 = {
            "/sys/fs/cgroup/cpu/cpuacct.usage": "2000000000\n",
            "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "200000\n",
            "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000\n",
        }
        time_values = [100_000_000_000, 200_000_000_000]
        time_idx = {"i": 0}

        def mock_monotonic_ns():
            val = time_values[time_idx["i"]]
            time_idx["i"] += 1
            return val

        with (
            patch.object(Path, "exists", _mock_path_exists(exists_map)),
            patch.object(Path, "read_text", _mock_path_read_text(read_map_1)),
            patch("rock.utils.cgroup_stats.time.monotonic_ns", mock_monotonic_ns),
        ):
            stats.cpu_percent()

        with (
            patch.object(Path, "exists", _mock_path_exists(exists_map)),
            patch.object(Path, "read_text", _mock_path_read_text(read_map_2)),
            patch("rock.utils.cgroup_stats.time.monotonic_ns", mock_monotonic_ns),
        ):
            result = stats.cpu_percent()

        # delta_usage = 1_000_000_000 ns, delta_time = 100_000_000_000 ns
        # quota = 200000/100000 = 2.0 CPUs
        # pct = (1_000_000_000 / 100_000_000_000) / 2.0 * 100 = 0.5%
        assert result == 0.5

    def test_fallback_to_psutil_when_no_cgroup(self):
        stats = CgroupCpuStats()
        stats._cgroup_version = 0
        with patch("rock.utils.cgroup_stats.psutil.cpu_percent", return_value=42.0):
            assert stats.cpu_percent() == 42.0

    def test_capped_at_100(self):
        stats = CgroupCpuStats()
        stats._cgroup_version = 2
        read_map_1 = {
            "/sys/fs/cgroup/cpu.stat": "usage_usec 0\n",
            "/sys/fs/cgroup/cpu.max": "100000 100000\n",
        }
        read_map_2 = {
            "/sys/fs/cgroup/cpu.stat": "usage_usec 200000000\n",
            "/sys/fs/cgroup/cpu.max": "100000 100000\n",
        }
        time_values = [100_000_000_000, 200_000_000_000]
        time_idx = {"i": 0}

        def mock_monotonic_ns():
            val = time_values[time_idx["i"]]
            time_idx["i"] += 1
            return val

        with (
            patch.object(Path, "read_text", _mock_path_read_text(read_map_1)),
            patch("rock.utils.cgroup_stats.time.monotonic_ns", mock_monotonic_ns),
        ):
            stats.cpu_percent()

        with (
            patch.object(Path, "read_text", _mock_path_read_text(read_map_2)),
            patch("rock.utils.cgroup_stats.time.monotonic_ns", mock_monotonic_ns),
        ):
            result = stats.cpu_percent()

        assert result == 100.0

    def test_zero_delta_time_returns_zero(self):
        stats = CgroupCpuStats()
        stats._cgroup_version = 2
        read_map = {"/sys/fs/cgroup/cpu.stat": "usage_usec 1000000\n"}
        same_time = 100_000_000_000

        with (
            patch.object(Path, "read_text", _mock_path_read_text(read_map)),
            patch("rock.utils.cgroup_stats.time.monotonic_ns", return_value=same_time),
        ):
            stats.cpu_percent()
            result = stats.cpu_percent()

        assert result == 0.0

    def test_negative_delta_usage_returns_zero(self):
        stats = CgroupCpuStats()
        stats._cgroup_version = 2
        read_map_1 = {"/sys/fs/cgroup/cpu.stat": "usage_usec 2000000\n"}
        read_map_2 = {
            "/sys/fs/cgroup/cpu.stat": "usage_usec 1000000\n",
            "/sys/fs/cgroup/cpu.max": "100000 100000\n",
        }
        time_values = [100_000_000_000, 200_000_000_000]
        time_idx = {"i": 0}

        def mock_monotonic_ns():
            val = time_values[time_idx["i"]]
            time_idx["i"] += 1
            return val

        with (
            patch.object(Path, "read_text", _mock_path_read_text(read_map_1)),
            patch("rock.utils.cgroup_stats.time.monotonic_ns", mock_monotonic_ns),
        ):
            stats.cpu_percent()

        with (
            patch.object(Path, "read_text", _mock_path_read_text(read_map_2)),
            patch("rock.utils.cgroup_stats.time.monotonic_ns", mock_monotonic_ns),
        ):
            result = stats.cpu_percent()

        assert result == 0.0


# ---------- CPU quota ----------


class TestCpuQuota:
    def test_v2_unlimited(self):
        stats = CgroupCpuStats()
        stats._cgroup_version = 2
        read_map = {"/sys/fs/cgroup/cpu.max": "max 100000\n"}
        with (
            patch.object(Path, "read_text", _mock_path_read_text(read_map)),
            patch("rock.utils.cgroup_stats.os.cpu_count", return_value=4),
        ):
            assert stats._read_cpu_quota() == 4.0

    def test_v1_unlimited(self):
        stats = CgroupCpuStats()
        stats._cgroup_version = 1
        read_map = {
            "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "-1\n",
            "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000\n",
        }
        with (
            patch.object(Path, "read_text", _mock_path_read_text(read_map)),
            patch("rock.utils.cgroup_stats.os.cpu_count", return_value=8),
        ):
            assert stats._read_cpu_quota() == 8.0

    def test_fallback_on_error(self):
        stats = CgroupCpuStats()
        stats._cgroup_version = 2
        with (
            patch.object(Path, "read_text", side_effect=PermissionError),
            patch("rock.utils.cgroup_stats.os.cpu_count", return_value=2),
        ):
            assert stats._read_cpu_quota() == 2.0
