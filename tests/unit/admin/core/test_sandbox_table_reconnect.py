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

    _OUTAGE_SECONDS = 4

    def _do_pg_restart_blocking(self, restartable_pg) -> None:
        """Stop, hold the outage open for _OUTAGE_SECONDS, then start.

        Runs in an executor so the asyncio loop stays free to drive the
        retry/back-off path inside SandboxTable while PG is down.
        Cumulative back-off in the production wrapper (1+2+4+8 = 15s) must
        exceed _OUTAGE_SECONDS for the test to pass; a no-sleep retry fires
        immediately, finds the DB still down, and fails.
        """
        import time

        container = restartable_pg["container"]
        container.exec_run(f"su postgres -c 'pg_ctl stop -D {_PGDATA} -m fast'")
        time.sleep(self._OUTAGE_SECONDS)
        container.exec_run(f"su postgres -c 'pg_ctl start -D {_PGDATA} -l /tmp/pg.log'")
        _wait_pg_ready_sql(container, _PGUSER, _PGDB)

    async def test_retry_recovers_after_pg_restart(self, table, restartable_pg):
        import asyncio

        await table.create("pgr-1", {"user_id": "bob", "create_time": "2025-01-01T00:00:00Z"})
        await table.list_by_in("sandbox_id", ["pgr-1"])  # warm pool

        # Kick off the outage in the background so the query below races against it.
        restart_task = asyncio.create_task(asyncio.to_thread(self._do_pg_restart_blocking, restartable_pg))

        # Let `pg_ctl stop` actually land and the asyncpg reader observe the RST
        # before issuing the query; the query must therefore traverse the outage
        # window via the retry decorator's back-off.
        await asyncio.sleep(0.5)

        result = await table.list_by_in("sandbox_id", ["pgr-1"])

        await restart_task
        assert len(result) == 1
        assert result[0]["sandbox_id"] == "pgr-1"
