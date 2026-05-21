import json
from collections import namedtuple
from unittest.mock import mock_open, patch

import pytest

from rock.rocklet.linux import LinuxRocklet

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free", "percent"])


@pytest.fixture
def runtime():
    return LinuxRocklet()


@pytest.mark.asyncio
async def test_get_statistics_returns_all_disk_fields(runtime):
    with (
        patch.object(runtime._cgroup_cpu, "cpu_percent", return_value=10.0),
        patch("psutil.virtual_memory") as mock_vmem,
        patch("psutil.disk_usage") as mock_disk,
        patch("psutil.net_io_counters") as mock_net,
        patch("os.path.exists", return_value=True),
        patch.dict("os.environ", {"ROCK_LOGGING_PATH": "/data/logs", "ROCK_KATA_RUNTIME": "true"}),
        patch("builtins.open", mock_open(read_data=json.dumps({"data-root": "/var/lib/docker"}))),
    ):
        mock_vmem.return_value = type("obj", (object,), {"percent": 50.0})()
        mock_net.return_value = type("obj", (object,), {"bytes_recv": 100, "bytes_sent": 200})()
        mock_disk.side_effect = lambda path: {
            "/": DiskUsage(total=100, used=75, free=25, percent=75.0),
            "/data/logs": DiskUsage(total=100, used=60, free=40, percent=60.0),
            "/var/lib/docker": DiskUsage(total=100, used=80, free=20, percent=80.0),
        }[path]

        stats = await runtime.get_statistics()

    assert stats["cpu"] == 10.0
    assert stats["mem"] == 50.0
    assert stats["disk"] == 75.0
    assert stats["disk_log_percent"] == 60.0
    assert stats["disk_dind_percent"] == 80.0
    assert stats["net"] == 300


@pytest.mark.asyncio
async def test_get_statistics_no_kata_runtime(runtime):
    with (
        patch.object(runtime._cgroup_cpu, "cpu_percent", return_value=5.0),
        patch("psutil.virtual_memory") as mock_vmem,
        patch("psutil.disk_usage") as mock_disk,
        patch("psutil.net_io_counters") as mock_net,
        patch("os.path.exists", return_value=True),
        patch.dict("os.environ", {"ROCK_LOGGING_PATH": "/data/logs"}, clear=False),
    ):
        mock_vmem.return_value = type("obj", (object,), {"percent": 40.0})()
        mock_net.return_value = type("obj", (object,), {"bytes_recv": 50, "bytes_sent": 50})()
        mock_disk.side_effect = lambda path: {
            "/": DiskUsage(total=100, used=30, free=70, percent=30.0),
            "/data/logs": DiskUsage(total=100, used=20, free=80, percent=20.0),
        }[path]

        # Ensure ROCK_KATA_RUNTIME is not set
        import os

        os.environ.pop("ROCK_KATA_RUNTIME", None)

        stats = await runtime.get_statistics()

    assert stats["disk_dind_percent"] == 0.0
    assert stats["disk"] == 30.0
    assert stats["disk_log_percent"] == 20.0


@pytest.mark.asyncio
async def test_get_statistics_log_path_not_exists(runtime):
    with (
        patch.object(runtime._cgroup_cpu, "cpu_percent", return_value=5.0),
        patch("psutil.virtual_memory") as mock_vmem,
        patch("psutil.disk_usage") as mock_disk,
        patch("psutil.net_io_counters") as mock_net,
        patch("os.path.exists", return_value=False),
        patch.dict("os.environ", {}, clear=False),
    ):
        mock_vmem.return_value = type("obj", (object,), {"percent": 40.0})()
        mock_net.return_value = type("obj", (object,), {"bytes_recv": 10, "bytes_sent": 10})()
        mock_disk.return_value = DiskUsage(total=100, used=50, free=50, percent=50.0)

        import os

        os.environ.pop("ROCK_KATA_RUNTIME", None)

        stats = await runtime.get_statistics()

    assert stats["disk_log_percent"] == 0.0
    assert stats["disk_dind_percent"] == 0.0


@pytest.mark.asyncio
async def test_get_statistics_log_disk_oserror(runtime):
    def disk_usage_side_effect(path):
        if path == "/":
            return DiskUsage(total=100, used=50, free=50, percent=50.0)
        raise OSError("Permission denied")

    with (
        patch.object(runtime._cgroup_cpu, "cpu_percent", return_value=5.0),
        patch("psutil.virtual_memory") as mock_vmem,
        patch("psutil.disk_usage", side_effect=disk_usage_side_effect),
        patch("psutil.net_io_counters") as mock_net,
        patch("os.path.exists", return_value=True),
        patch.dict("os.environ", {"ROCK_LOGGING_PATH": "/data/logs"}, clear=False),
    ):
        mock_vmem.return_value = type("obj", (object,), {"percent": 40.0})()
        mock_net.return_value = type("obj", (object,), {"bytes_recv": 10, "bytes_sent": 10})()

        import os

        os.environ.pop("ROCK_KATA_RUNTIME", None)

        stats = await runtime.get_statistics()

    assert stats["disk_log_percent"] == 0.0


@pytest.mark.asyncio
async def test_get_statistics_dind_disk_oserror(runtime):
    def disk_usage_side_effect(path):
        if path == "/":
            return DiskUsage(total=100, used=50, free=50, percent=50.0)
        if path == "/data/logs":
            return DiskUsage(total=100, used=30, free=70, percent=30.0)
        raise OSError("Permission denied")

    with (
        patch.object(runtime._cgroup_cpu, "cpu_percent", return_value=5.0),
        patch("psutil.virtual_memory") as mock_vmem,
        patch("psutil.disk_usage", side_effect=disk_usage_side_effect),
        patch("psutil.net_io_counters") as mock_net,
        patch("os.path.exists", return_value=True),
        patch.dict(
            "os.environ", {"ROCK_LOGGING_PATH": "/data/logs", "ROCK_KATA_RUNTIME": "true"}, clear=False
        ),
        patch("builtins.open", mock_open(read_data=json.dumps({"data-root": "/var/lib/docker"}))),
    ):
        mock_vmem.return_value = type("obj", (object,), {"percent": 40.0})()
        mock_net.return_value = type("obj", (object,), {"bytes_recv": 10, "bytes_sent": 10})()

        stats = await runtime.get_statistics()

    assert stats["disk_dind_percent"] == 0.0
    assert stats["disk_log_percent"] == 30.0


class TestGetDockerDataRoot:
    def test_reads_data_root_from_daemon_json(self):
        daemon_config = json.dumps({"data-root": "/mnt/docker-data"})
        with patch("builtins.open", mock_open(read_data=daemon_config)):
            result = LinuxRocklet._get_docker_data_root()
        assert result == "/mnt/docker-data"

    def test_returns_default_when_no_data_root_key(self):
        daemon_config = json.dumps({"storage-driver": "overlay2"})
        with patch("builtins.open", mock_open(read_data=daemon_config)):
            result = LinuxRocklet._get_docker_data_root()
        assert result == "/var/lib/docker"

    def test_returns_default_when_file_not_found(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = LinuxRocklet._get_docker_data_root()
        assert result == "/var/lib/docker"

    def test_returns_default_when_invalid_json(self):
        with patch("builtins.open", mock_open(read_data="not valid json")):
            result = LinuxRocklet._get_docker_data_root()
        assert result == "/var/lib/docker"
