"""Disposable PostgreSQL fixture for snapshot catalog contract tests."""

import time
import uuid

import pytest

from tests.unit.conftest import _docker_detect_network, _docker_resolve_host_port, _docker_start_container


@pytest.fixture
def snapshot_postgres_url():
    import docker

    client = docker.from_env()
    network = _docker_detect_network(client)
    name = f"rock-part1-pg-{uuid.uuid4().hex[:12]}"
    container = _docker_start_container(
        client, image="postgres:17.4", name=name, network_name=network, internal_port=5432,
        environment={"POSTGRES_USER": "rock", "POSTGRES_PASSWORD": "rock-test", "POSTGRES_DB": "rock"},
    )
    try:
        for _ in range(60):
            code, _ = container.exec_run("psql -U rock -d rock -c 'SELECT 1'")
            if code == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("PostgreSQL did not become ready")
        host, port = _docker_resolve_host_port(container, network, 5432)
        yield f"postgresql://rock:rock-test@{host}:{port}/rock"
    finally:
        container.remove(force=True)
        client.close()
