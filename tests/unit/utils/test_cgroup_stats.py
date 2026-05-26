"""Tests for rock.utils.cgroup_stats — container-aware CPU/memory metrics."""

from unittest.mock import patch

from rock.utils.cgroup_stats import CgroupCpuStats, CgroupMemStats, Path


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


# ---------- Memory cgroup version detection ----------


class TestMemDetectCgroupVersion:
    def test_detects_v2(self):
        stats = CgroupMemStats()
        exists_map = {"/sys/fs/cgroup/cgroup.controllers": True}
        with patch.object(Path, "exists", _mock_path_exists(exists_map)):
            assert stats._detect_cgroup_version() == 2

    def test_detects_v1(self):
        stats = CgroupMemStats()
        exists_map = {
            "/sys/fs/cgroup/cgroup.controllers": False,
            "/sys/fs/cgroup/memory/memory.usage_in_bytes": True,
        }
        with patch.object(Path, "exists", _mock_path_exists(exists_map)):
            assert stats._detect_cgroup_version() == 1

    def test_detects_no_cgroup(self):
        stats = CgroupMemStats()
        exists_map = {
            "/sys/fs/cgroup/cgroup.controllers": False,
            "/sys/fs/cgroup/memory/memory.usage_in_bytes": False,
        }
        with patch.object(Path, "exists", _mock_path_exists(exists_map)):
            assert stats._detect_cgroup_version() == 0

    def test_caches_result(self):
        stats = CgroupMemStats()
        stats._cgroup_version = 2
        assert stats._detect_cgroup_version() == 2


# ---------- Memory percent ----------


class TestMemPercent:
    def test_v2_mem_calculation(self):
        stats = CgroupMemStats()
        stats._cgroup_version = 2
        read_map = {
            "/sys/fs/cgroup/memory.current": "524288000\n",
            "/sys/fs/cgroup/memory.max": "1073741824\n",
        }
        with patch.object(Path, "read_text", _mock_path_read_text(read_map)):
            result = stats.mem_percent()
        # 524288000 / 1073741824 * 100 = 48.8%
        assert result == 48.8

    def test_v1_mem_calculation(self):
        stats = CgroupMemStats()
        stats._cgroup_version = 1
        read_map = {
            "/sys/fs/cgroup/memory/memory.usage_in_bytes": "734003200\n",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes": "1073741824\n",
        }
        with patch.object(Path, "read_text", _mock_path_read_text(read_map)):
            result = stats.mem_percent()
        # 734003200 / 1073741824 * 100 = 68.4%
        assert result == 68.4

    def test_fallback_to_psutil_when_no_cgroup(self):
        stats = CgroupMemStats()
        stats._cgroup_version = 0
        with patch("rock.utils.cgroup_stats.psutil.virtual_memory") as mock_vmem:
            mock_vmem.return_value.percent = 55.3
            assert stats.mem_percent() == 55.3

    def test_fallback_to_psutil_when_unlimited_v2(self):
        stats = CgroupMemStats()
        stats._cgroup_version = 2
        read_map = {
            "/sys/fs/cgroup/memory.current": "524288000\n",
            "/sys/fs/cgroup/memory.max": "max\n",
        }
        with (
            patch.object(Path, "read_text", _mock_path_read_text(read_map)),
            patch("rock.utils.cgroup_stats.psutil.virtual_memory") as mock_vmem,
        ):
            mock_vmem.return_value.percent = 33.0
            assert stats.mem_percent() == 33.0

    def test_fallback_to_psutil_when_unlimited_v1(self):
        stats = CgroupMemStats()
        stats._cgroup_version = 1
        read_map = {
            "/sys/fs/cgroup/memory/memory.usage_in_bytes": "524288000\n",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712\n",
        }
        with (
            patch.object(Path, "read_text", _mock_path_read_text(read_map)),
            patch("rock.utils.cgroup_stats.psutil.virtual_memory") as mock_vmem,
        ):
            mock_vmem.return_value.percent = 45.0
            assert stats.mem_percent() == 45.0

    def test_capped_at_100(self):
        stats = CgroupMemStats()
        stats._cgroup_version = 2
        stats._mem_limit = 1073741824
        read_map = {
            "/sys/fs/cgroup/memory.current": "2147483648\n",
        }
        with patch.object(Path, "read_text", _mock_path_read_text(read_map)):
            result = stats.mem_percent()
        assert result == 100.0

    def test_caches_mem_limit(self):
        stats = CgroupMemStats()
        stats._cgroup_version = 2
        read_map = {
            "/sys/fs/cgroup/memory.current": "524288000\n",
            "/sys/fs/cgroup/memory.max": "1073741824\n",
        }
        with patch.object(Path, "read_text", _mock_path_read_text(read_map)):
            stats.mem_percent()
        assert stats._mem_limit == 1073741824

    def test_fallback_to_psutil_on_read_error(self):
        stats = CgroupMemStats()
        stats._cgroup_version = 2
        with (
            patch.object(Path, "read_text", side_effect=PermissionError),
            patch("rock.utils.cgroup_stats.psutil.virtual_memory") as mock_vmem,
        ):
            mock_vmem.return_value.percent = 60.0
            assert stats.mem_percent() == 60.0

    def test_v2_subtracts_inactive_file_cache(self):
        """memory.current includes page cache; inactive_file should be subtracted."""
        stats = CgroupMemStats()
        stats._cgroup_version = 2
        read_map = {
            "/sys/fs/cgroup/memory.current": "1073741824\n",
            "/sys/fs/cgroup/memory.stat": "anon 524288000\nfile 400000000\ninactive_file 262144000\nactive_file 137856000\n",
            "/sys/fs/cgroup/memory.max": "2147483648\n",
        }
        with patch.object(Path, "read_text", _mock_path_read_text(read_map)):
            result = stats.mem_percent()
        # (1073741824 - 262144000) / 2147483648 * 100 = 37.8%
        assert result == 37.8

    def test_v1_subtracts_total_inactive_file_cache(self):
        """memory.usage_in_bytes includes page cache; total_inactive_file should be subtracted."""
        stats = CgroupMemStats()
        stats._cgroup_version = 1
        read_map = {
            "/sys/fs/cgroup/memory/memory.usage_in_bytes": "1073741824\n",
            "/sys/fs/cgroup/memory/memory.stat": "cache 400000000\ntotal_cache 400000000\ntotal_inactive_file 262144000\n",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes": "2147483648\n",
        }
        with patch.object(Path, "read_text", _mock_path_read_text(read_map)):
            result = stats.mem_percent()
        # (1073741824 - 262144000) / 2147483648 * 100 = 37.8%
        assert result == 37.8

    def test_v2_no_subtraction_when_memory_stat_missing(self):
        """If memory.stat is unreadable, fall back to raw usage rather than crashing."""
        stats = CgroupMemStats()
        stats._cgroup_version = 2
        # No memory.stat entry — _mock_path_read_text raises FileNotFoundError.
        read_map = {
            "/sys/fs/cgroup/memory.current": "524288000\n",
            "/sys/fs/cgroup/memory.max": "1073741824\n",
        }
        with patch.object(Path, "read_text", _mock_path_read_text(read_map)):
            result = stats.mem_percent()
        assert result == 48.8

    def test_v2_inactive_file_larger_than_usage_clamped_to_zero(self):
        """Sanity guard: subtraction must not produce a negative usage."""
        stats = CgroupMemStats()
        stats._cgroup_version = 2
        read_map = {
            "/sys/fs/cgroup/memory.current": "100\n",
            "/sys/fs/cgroup/memory.stat": "inactive_file 9999\n",
            "/sys/fs/cgroup/memory.max": "1000\n",
        }
        with patch.object(Path, "read_text", _mock_path_read_text(read_map)):
            result = stats.mem_percent()
        assert result == 0.0

    def test_v2_ignores_malformed_memory_stat_lines(self):
        """memory.stat parser should skip blank/malformed lines without raising."""
        stats = CgroupMemStats()
        stats._cgroup_version = 2
        read_map = {
            "/sys/fs/cgroup/memory.current": "1000\n",
            "/sys/fs/cgroup/memory.stat": "\nmalformed\ninactive_file 200\nanon\n",
            "/sys/fs/cgroup/memory.max": "10000\n",
        }
        with patch.object(Path, "read_text", _mock_path_read_text(read_map)):
            result = stats.mem_percent()
        # (1000 - 200) / 10000 * 100 = 8.0%
        assert result == 8.0
