from __future__ import annotations

import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.admin.snapd.client import SnapdResultUnknown
from rock.admin.snapd.worker_client import _SnapdClientPool


@pytest.mark.parametrize("first_module", ["rock.admin.snapd.worker_client", "rock.sandbox.operator.snapd"])
def test_worker_client_and_operator_import_in_either_order(first_module: str) -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib; "
            f"importlib.import_module({first_module!r}); "
            "from rock.admin.snapd.worker_client import _SnapdClientPool; "
            "from rock.sandbox.operator.snapd import SnapdPlacement; "
            "assert SnapdPlacement.__module__ == 'rock.sandbox.operator.snapd'",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_worker_client_does_not_load_ray_or_database_dependencies() -> None:
    subprocess.run(
        [sys.executable, "-c", """
import importlib.abc
import sys

class NoControlPlaneImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in {"ray", "sqlalchemy", "rock.admin.core.ray_service"}:
            raise AssertionError(f"transport loaded control-plane dependency: {fullname}")

sys.meta_path.insert(0, NoControlPlaneImports())
from rock.admin.snapd.worker_client import _SnapdClientPool
assert _SnapdClientPool is not None
"""],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


class _Client:
    def __init__(self, url: str) -> None:
        self.url = url
        self.aclose = AsyncMock()


def test_worker_client_reuses_known_ip_without_discovery_or_healthcheck(monkeypatch) -> None:
    factory = MagicMock(side_effect=_Client)
    monkeypatch.setattr("rock.admin.snapd.worker_client.SnapdClient", factory)
    pool = _SnapdClientPool()

    first = pool.client("10.0.0.1")
    assert pool.client("10.0.0.1") is first
    second = pool.client("10.0.0.2")

    assert first.url == "http://10.0.0.1:22556"
    assert second.url == "http://10.0.0.2:22556"
    assert first is not second
    assert factory.call_count == 2


def test_worker_client_normalizes_and_brackets_ipv6(monkeypatch) -> None:
    factory = MagicMock(side_effect=_Client)
    monkeypatch.setattr("rock.admin.snapd.worker_client.SnapdClient", factory)
    pool = _SnapdClientPool()

    client = pool.client("2001:db8::1")

    assert pool.client("2001:0db8:0000:0000:0000:0000:0000:0001") is client
    assert client.url == "http://[2001:db8::1]:22556"
    factory.assert_called_once_with("http://[2001:db8::1]:22556")


@pytest.mark.parametrize("host_ip", [
    "", "worker.example", "http://10.0.0.1", "10.0.0.1:22556", None, 123,
    "0.0.0.0", "::", "224.0.0.1", "ff02::1",
])
def test_worker_client_rejects_invalid_or_non_unicast_address_before_connecting(host_ip, monkeypatch) -> None:
    factory = MagicMock(side_effect=_Client)
    monkeypatch.setattr("rock.admin.snapd.worker_client.SnapdClient", factory)
    pool = _SnapdClientPool()

    with pytest.raises(SnapdResultUnknown, match="IP literal|connectable unicast"):
        pool.client(host_ip)

    factory.assert_not_called()


async def test_worker_client_closes_cached_clients_once_and_releases_cache(monkeypatch) -> None:
    factory = MagicMock(side_effect=_Client)
    monkeypatch.setattr("rock.admin.snapd.worker_client.SnapdClient", factory)
    pool = _SnapdClientPool()
    first = pool.client("10.0.0.1")
    second = pool.client("10.0.0.2")

    await pool.aclose()
    await pool.aclose()

    first.aclose.assert_awaited_once()
    second.aclose.assert_awaited_once()
    assert pool.client("10.0.0.1") is not first
    assert factory.call_count == 3
    await pool.aclose()


async def test_worker_client_uses_snapd_client_by_default(monkeypatch) -> None:
    factory = MagicMock(side_effect=_Client)
    monkeypatch.setattr("rock.admin.snapd.worker_client.SnapdClient", factory)
    pool = _SnapdClientPool()

    pool.client("10.0.0.1")

    factory.assert_called_once_with("http://10.0.0.1:22556")
    await pool.aclose()
