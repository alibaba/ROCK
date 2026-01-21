# rock/admin/scheduler/worker_client.py

import httpx

from rock.actions.sandbox.response import CommandResponse
from rock.deployments.constants import Port
from rock.logger import init_logger

logger = init_logger(name="image_clean", file_name="scheduler.log")


class WorkerClient:
    """HTTP client for worker nodes."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    async def execute(self, ip: str, command: str, port: int = Port.PROXY.value) -> CommandResponse:
        """Call worker's execute endpoint."""
        url = f"http://{ip}:{port}/execute"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
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
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json={"path": file_path, "content": content})
            return resp.json()

    async def read_file(self, ip: str, file_path: str, port: int = Port.PROXY.value) -> str | None:
        """Read file content from worker."""
        url = f"http://{ip}:{port}/read_file"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
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
