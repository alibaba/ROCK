# rock/admin/scheduler/worker_client.py

import httpx

from rock.actions.sandbox.response import CommandResponse
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.deployments.constants import Port
from rock.logger import init_logger

logger = init_logger(name="worker_client", file_name=SCHEDULER_LOG_NAME)


class WorkerClient:
    """HTTP client for worker nodes with connection pooling."""

    def __init__(self, timeout: int = 85, max_connections: int = 200, max_keepalive_connections: int = 20):
        """
        Initialize WorkerClient with connection pool settings.

        Args:
            timeout: Request timeout in seconds
            max_connections: Maximum number of concurrent connections
            max_keepalive_connections: Maximum number of keep-alive connections
        """
        self.timeout = timeout
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared HTTP client with connection pooling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=self._limits,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client and release connections."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def execute(self, ip: str, command: str, port: int = Port.PROXY.value) -> CommandResponse:
        """Call worker's execute endpoint."""
        url = f"http://{ip}:{port}/execute"
        client = await self._get_client()
        resp = await client.post(url, json={"command": command, "shell": True})
        execute_resp = CommandResponse(**resp.json())
        if execute_resp.exit_code != 0:
            error_msg = f"execute command [{command}] error, caused by [{execute_resp.stderr}]"
            logger.error(error_msg)
            raise RuntimeError(error_msg)
        return execute_resp

    async def write_file(self, ip: str, file_path: str, content: str, port: int = Port.PROXY.value) -> dict:
        """Call worker's write_file endpoint."""
        url = f"http://{ip}:{port}/write_file"
        client = await self._get_client()
        resp = await client.post(url, json={"path": file_path, "content": content})
        return resp.json()

    async def read_file(self, ip: str, file_path: str, port: int = Port.PROXY.value) -> str | None:
        """Read file content from worker."""
        url = f"http://{ip}:{port}/read_file"
        client = await self._get_client()
        resp = await client.post(url, json={"path": file_path})
        if resp.status_code == 200:
            return resp.json().get("content")
        error_msg = f"read file [{file_path}] error"
        logger.error(error_msg)
        raise RuntimeError(error_msg)

    async def check_pid_exists(self, ip: str, pid: int, port: int = Port.PROXY.value) -> bool:
        """Check if a process exists on worker."""
        result = await self.execute(ip, f"kill -0 {pid} 2>/dev/null && echo 'exists' || echo 'not_exists'", port)
        return result.stdout.strip() == "exists"
