"""Cached HTTP transports for known worker-local rock-snapd services."""

from __future__ import annotations

import ipaddress

from rock.admin.snapd.client import SnapdClient, SnapdResultUnknown

SNAPD_PORT = 22_556


class _SnapdClientPool:
    """Reuse clients by worker IP; placement and scheduling belong to callers."""

    def __init__(self) -> None:
        self._clients: dict[str, SnapdClient] = {}

    def client(self, host_ip: str) -> SnapdClient:
        if not isinstance(host_ip, str):
            raise SnapdResultUnknown("snapd worker address is not an IP literal")
        try:
            address = ipaddress.ip_address(host_ip)
        except ValueError as error:
            raise SnapdResultUnknown("snapd worker address is not an IP literal") from error
        if address.is_unspecified or address.is_multicast:
            raise SnapdResultUnknown("snapd worker address is not connectable unicast")
        host_ip = str(address)
        existing = self._clients.get(host_ip)
        if existing is not None:
            return existing
        host = f"[{host_ip}]" if address.version == 6 else host_ip
        client = SnapdClient(f"http://{host}:{SNAPD_PORT}")
        self._clients[host_ip] = client
        return client

    async def aclose(self) -> None:
        clients = tuple(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.aclose()
