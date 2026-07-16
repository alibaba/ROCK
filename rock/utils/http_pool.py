"""Config-driven registry of persistent httpx.AsyncClient pools.

Usage:
    pool = HttpPoolManager(rock_config.http_pools)
    client = pool.get("probe")  # -> httpx.AsyncClient, reused on subsequent calls
    await pool.aclose_all()     # graceful shutdown

Each pool name maps to an HttpPoolConfig in RockConfig.http_pools. Clients are
created lazily on first access and reused for the process lifetime.
"""

import httpx

from rock.config import HttpPoolConfig
from rock.logger import init_logger

logger = init_logger(__name__)


class HttpPoolManager:
    def __init__(self, pool_configs: dict[str, HttpPoolConfig]) -> None:
        self._configs = pool_configs
        self._clients: dict[str, httpx.AsyncClient] = {}

    def get(self, name: str) -> httpx.AsyncClient:
        """Return the named pool's client, creating it on first access."""
        client = self._clients.get(name)
        if client is not None and not client.is_closed:
            return client
        cfg = self._configs.get(name)
        if cfg is None:
            cfg = HttpPoolConfig()  # sensible default
            logger.warning(f"http pool '{name}' not configured, using defaults")
        self._clients[name] = httpx.AsyncClient(
            timeout=cfg.timeout,
            limits=httpx.Limits(
                max_connections=cfg.max_connections,
                max_keepalive_connections=cfg.max_keepalive_connections,
            ),
        )
        logger.info(
            f"http pool '{name}' created: timeout={cfg.timeout}s, "
            f"max_conn={cfg.max_connections}, max_keepalive={cfg.max_keepalive_connections}"
        )
        return self._clients[name]

    def get_pool_stats(self, name: str | None = None) -> dict[str, dict[str, int]]:
        """Return connection stats for the specified pool, or all pools if name is None.

        Accesses httpcore internals — safe for monitoring but not part of the
        public httpx API, so guard against AttributeError.
        """
        stats = {}
        if name is not None:
            client = self._clients.get(name)
            if client is None or client.is_closed:
                return stats
            try:
                pool = client._transport._pool
                connections = pool.connections
                active = sum(1 for c in connections if not c.is_idle() and not c.is_closed())
                stats[name] = {
                    "active": active,
                    "idle": sum(1 for c in connections if c.is_idle() and not c.is_closed()),
                    "pending_requests": max(0, len(pool._requests) - active),
                }
            except (AttributeError, TypeError):
                logger.debug(f"http pool '{name}': unable to read pool internals")
            return stats

        for pool_name, client in self._clients.items():
            if client.is_closed:
                continue
            try:
                pool = client._transport._pool
                connections = pool.connections
                active = sum(1 for c in connections if not c.is_idle() and not c.is_closed())
                stats[pool_name] = {
                    "active": active,
                    "idle": sum(1 for c in connections if c.is_idle() and not c.is_closed()),
                    "pending_requests": max(0, len(pool._requests) - active),
                }
            except (AttributeError, TypeError):
                logger.debug(f"http pool '{pool_name}': unable to read pool internals")
        return stats

    async def aclose_all(self) -> None:
        """Close all open clients. Safe to call multiple times."""
        for name, client in self._clients.items():
            if not client.is_closed:
                await client.aclose()
                logger.info(f"http pool '{name}' closed")
        self._clients.clear()
