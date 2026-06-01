"""
Integration tests for the docker-start side helpers used by ``restart()``:

- ``_docker_start()``                — attached ``docker start -a`` (also used by start())
- ``_get_rocklet_port_from_inspect()`` — recovers the rocklet host port from a live container

We don't drive ``restart()`` end-to-end here (that needs an HTTP probe against
rocklet, covered by ``test_restart_e2e.py``); instead we verify the building
blocks behave correctly against real docker so the test catches docker CLI
format regressions as well as our parsing logic.
"""

from __future__ import annotations

import socket
import subprocess
import time
import uuid

import pytest

from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment
from tests.integration.conftest import SKIP_IF_NO_DOCKER


def _free_port() -> int:
    """Synchronous helper: bind to port 0 to get an OS-allocated free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _docker_state(name: str) -> str:
    """Read container's State.Status via docker inspect; "unknown" on failure."""
    result = subprocess.run(
        ["docker", "inspect", "--format={{.State.Status}}", name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


pytestmark = [pytest.mark.need_docker, SKIP_IF_NO_DOCKER]


@pytest.fixture
def container_name() -> str:
    """Unique container name so parallel test runs don't clash."""
    return f"diag-test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def deployment(container_name: str) -> DockerDeployment:
    cfg = DockerDeploymentConfig(
        container_name=container_name,
        image="python:3.11",
        memory="512m",
        cpus=0.5,
    )
    return cfg.get_deployment()


class TestDockerStartHelpers:
    """Tests for the docker-start side helpers used by restart() — without
    requiring a real rocklet image."""

    def test_docker_start_revives_exited_container_with_same_id_and_ports(
        self, container_name: str, deployment: DockerDeployment
    ):
        # Create container with **explicit host:container port mapping** —
        # this mirrors DockerDeployment._docker_run() which always passes
        # `-p {host_port}:{container_port}`.  Without an explicit host port,
        # docker re-allocates a fresh host port on each start, so we must
        # match production behavior to test the "ports preserved" invariant.
        host_port = _free_port()
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
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
            cid_before = subprocess.run(
                ["docker", "inspect", "--format={{.Id}}", container_name],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            host_port_before = deployment._get_rocklet_port_from_inspect()
            assert host_port_before is not None

            # Stop, then docker start via the helper under test
            subprocess.run(["docker", "kill", container_name], check=True, capture_output=True, timeout=15)
            for _ in range(20):
                if _docker_state(container_name) == "exited":
                    break
                time.sleep(0.2)
            assert _docker_state(container_name) == "exited"

            deployment._docker_start()  # the function actually used by restart()

            # Container should be running again
            for _ in range(20):
                if _docker_state(container_name) == "running":
                    break
                time.sleep(0.2)
            assert _docker_state(container_name) == "running"

            cid_after = subprocess.run(
                ["docker", "inspect", "--format={{.Id}}", container_name],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            host_port_after = deployment._get_rocklet_port_from_inspect()

            # Critical invariants for restart semantics
            assert cid_after == cid_before, "container id must be preserved (same container, not new one)"
            assert host_port_after == host_port_before, "published port mapping must be preserved"
        finally:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=15)

    def test_get_rocklet_port_returns_none_when_no_22555_mapping(
        self, container_name: str, deployment: DockerDeployment
    ):
        # Container without `-p 22555` shouldn't expose port; helper returns None
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
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
            assert deployment._get_rocklet_port_from_inspect() is None
        finally:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=15)
