"""SandboxTable reconnect tests — PostgreSQL process restart inside the container.

Setup
-----
PID 1 of the container is ``sh`` blocked on ``sleep infinity``.
postgres runs as a background child.  ``pg_ctl stop / start`` restarts the
postgres process without touching the container, so the host port stays stable
and data is preserved (same PGDATA directory, new process).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from rock.admin.core.sandbox_table import SandboxTable

_PGUSER = "test"
_PGPASS = "test"
_PGDB = "testdb"
_PGDATA = "/var/lib/postgresql/data"


def _wait_pg_ready_sql(container, user: str, db: str, timeout: int = 30) -> None:
    """Two-stage wait: pg_isready (socket up) then SELECT 1 (queries accepted).

    Mirrors the logic in tests/unit/conftest.py::pg_container to close the
    startup race window between the socket accepting and WAL replay finishing.
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        code, _ = container.exec_run(f"pg_isready -U {user}")
        if code == 0:
            code, _ = container.exec_run(f'psql -U {user} -d {db} -c "SELECT 1"')
            if code == 0:
                return
        time.sleep(0.5)
    raise TimeoutError(f"PostgreSQL did not become ready within {timeout}s")


@pytest.mark.need_docker
class TestSandboxTablePgProcessRestart:
    """PostgreSQL process restart inside a running container.

    pool_pre_ping=False so the pool does NOT silently reconnect — the
    @_retry_on_disconnect decorator must handle recovery.
    """

    @pytest.fixture
    def restartable_pg(self):
        """Container where postgres runs as a background child of PID 1 (sleep infinity)."""
        import socket
        import uuid

        import docker

        client = docker.from_env()
        name = f"rock-test-pg-proc-{uuid.uuid4().hex[:8]}"

        hostname = socket.gethostname()
        try:
            current = client.containers.get(hostname)
            networks = current.attrs["NetworkSettings"]["Networks"]
            network_name = "bridge" if "bridge" in networks else next(iter(networks), None)
        except Exception:
            network_name = None

        env = {"POSTGRES_USER": _PGUSER, "POSTGRES_PASSWORD": _PGPASS, "POSTGRES_DB": _PGDB}
        run_kwargs = {
            "image": "postgres:16-alpine",
            "name": name,
            "detach": True,
            "environment": env,
            "entrypoint": ["sh", "-c"],
            # Single-element list: Docker passes the whole string as argv[1] to sh -c.
            # A plain string would be split on spaces, breaking the & operator.
            "command": ["docker-entrypoint.sh postgres & sleep infinity"],
        }
        if network_name:
            run_kwargs["network"] = network_name
        else:
            run_kwargs["ports"] = {"5432/tcp": None}

        container = client.containers.run(**run_kwargs)
        try:
            _wait_pg_ready_sql(container, _PGUSER, _PGDB)
            container.reload()
            if network_name:
                host = container.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]
                port = 5432
            else:
                host = "127.0.0.1"
                port = int(container.ports["5432/tcp"][0]["HostPort"])

            yield {
                "container": container,
                "url": f"postgresql://{_PGUSER}:{_PGPASS}@{host}:{port}/{_PGDB}",
            }
        finally:
            try:
                container.stop(timeout=5)
                container.remove()
            except Exception:
                pass

    @pytest.fixture
    async def table(self, restartable_pg):
        """pool_size=1, pool_pre_ping=False — decorator must handle stale connections."""
        from sqlalchemy.ext.asyncio import create_async_engine

        from rock.admin.core.schema import Base

        url = restartable_pg["url"].replace("postgresql://", "postgresql+asyncpg://")
        engine = create_async_engine(
            url,
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=False,
            connect_args={"statement_cache_size": 0},
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        provider = MagicMock()
        provider.engine = engine
        t = SandboxTable(provider)
        yield t
        await engine.dispose()

    async def _pg_restart_and_wait(self, restartable_pg) -> None:
        """Stop then start the postgres process; keep asyncio event loop alive.

        run_in_executor prevents blocking the event loop so asyncpg can process
        the TCP RST from the stopped backend while pg_ctl is running.
        The sleep afterwards lets asyncpg finish settling the connection to closed.
        """
        import asyncio

        container = restartable_pg["container"]

        def do_restart() -> None:
            container.exec_run(f"su postgres -c 'pg_ctl stop -D {_PGDATA} -m fast'")
            container.exec_run(f"su postgres -c 'pg_ctl start -D {_PGDATA} -l /tmp/pg.log'")
            _wait_pg_ready_sql(container, _PGUSER, _PGDB)

        await asyncio.get_event_loop().run_in_executor(None, do_restart)
        await asyncio.sleep(0.5)

    async def test_retry_recovers_after_pg_restart(self, table, restartable_pg):
        await table.create("pgr-1", {"user_id": "bob", "create_time": "2025-01-01T00:00:00Z"})
        await table.list_by_in("sandbox_id", ["pgr-1"])  # warm pool

        await self._pg_restart_and_wait(restartable_pg)

        result = await table.list_by_in("sandbox_id", ["pgr-1"])
        assert len(result) == 1
        assert result[0]["sandbox_id"] == "pgr-1"
