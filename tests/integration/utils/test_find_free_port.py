"""
Integration tests for find_free_port — covers the cross-process protection
added by querying `docker ps` for host ports already published by any container
(running OR stopped).

These tests use real docker (no mocks) so they validate both our parsing logic
and docker CLI output format.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import uuid

import pytest

from rock.utils import system as _system
from rock.utils.system import (
    _REGISTERED_PORTS,
    _get_docker_used_host_ports,
    find_free_port,
    refresh_docker_used_ports,
    release_port,
)
from tests.integration.conftest import SKIP_IF_NO_DOCKER

pytestmark = [pytest.mark.need_docker, SKIP_IF_NO_DOCKER]


@pytest.fixture(autouse=True)
def _clear_docker_used_ports_cache():
    # ``_get_docker_used_host_ports`` short-circuits to the module-level
    # ``_DOCKER_USED_PORTS`` snapshot when it is non-empty. Other tests in this
    # worker may have populated it via ``refresh_docker_used_ports`` /
    # ``do_port_mapping``; clear before and after each test so each one starts
    # from a clean slate.
    _system._DOCKER_USED_PORTS.clear()
    yield
    _system._DOCKER_USED_PORTS.clear()


def _free_port_via_os() -> int:
    """Get an OS-allocated free port (TOCTOU: port may be re-allocated soon)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def container_holding_port():
    """Spin up a container that publishes a chosen host port; yield (name, port)."""
    name = f"fft-{uuid.uuid4().hex[:10]}"
    host_port = _free_port_via_os()
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"{host_port}:22555",
            "--entrypoint",
            "",
            "python:3.11",
            "sh",
            "-c",
            "sleep 30",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    try:
        yield name, host_port
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)


class TestGetDockerUsedHostPorts:
    def test_lists_running_container_ports(self, container_holding_port):
        _name, port = container_holding_port
        used = _get_docker_used_host_ports()
        assert port in used, f"running container's host port {port} should be in used set"

    def test_lists_stopped_container_ports(self, container_holding_port):
        # Stopped containers still hold the host_port:container_port reservation
        # in docker metadata — must also be avoided by find_free_port.
        name, port = container_holding_port
        subprocess.run(["docker", "kill", name], check=True, capture_output=True, timeout=15)
        used = _get_docker_used_host_ports()
        assert port in used, f"stopped container's host port {port} should remain in used set"

    def test_returns_set_of_ints(self):
        # Sanity: format parsing shouldn't break even when no containers exist with our naming
        result = _get_docker_used_host_ports()
        assert isinstance(result, set)
        for p in result:
            assert isinstance(p, int)
            assert 0 < p < 65536


class TestFindFreePortAvoidsDockerPorts:
    # find_free_port reads _DOCKER_USED_PORTS as-is and does not refresh on
    # its own. Production callers (do_port_mapping) refresh upfront — these
    # tests mirror that contract by calling refresh_docker_used_ports() first.

    def test_does_not_return_port_held_by_running_container(self, container_holding_port):
        _name, blocked_port = container_holding_port
        refresh_docker_used_ports()
        try:
            ports = []
            for _ in range(5):
                p = asyncio.run(find_free_port())
                ports.append(p)
                assert p != blocked_port, f"find_free_port returned blocked port {p}"
        finally:
            for p in ports:
                release_port(p)

    def test_does_not_return_port_held_by_stopped_container(self, container_holding_port):
        # Same guarantee must hold for stopped containers (they will reclaim
        # the port on docker start).
        name, blocked_port = container_holding_port
        subprocess.run(["docker", "kill", name], check=True, capture_output=True, timeout=15)
        refresh_docker_used_ports()
        try:
            ports = []
            for _ in range(5):
                p = asyncio.run(find_free_port())
                ports.append(p)
                assert p != blocked_port, f"find_free_port returned blocked port {p}"
        finally:
            for p in ports:
                release_port(p)


class TestFindFreePortStillRespectsInProcessRegistry:
    def test_consecutive_calls_return_distinct_ports(self):
        """Same-process concurrent protection (the original behaviour) must still work."""
        ports = []
        try:
            for _ in range(5):
                p = asyncio.run(find_free_port())
                ports.append(p)
            assert len(ports) == len(set(ports)), f"got duplicates: {ports}"
        finally:
            for p in ports:
                release_port(p)

    def test_release_port_makes_it_reusable(self):
        p = asyncio.run(find_free_port())
        assert p in _REGISTERED_PORTS
        release_port(p)
        assert p not in _REGISTERED_PORTS
