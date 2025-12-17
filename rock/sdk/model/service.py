import asyncio
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx


class ModelService:
    def start_sandbox_service(self, model_service_type: str = "local") -> subprocess.Popen:
        """start sandbox service"""
        current_file = Path(__file__).resolve()
        service_dir = current_file.parent / "server"

        if not service_dir.exists():
            raise FileNotFoundError(f"Service directory not found: {service_dir}")

        process = subprocess.Popen([sys.executable, "-m", "main", "--type", model_service_type], cwd=str(service_dir))
        return process

    async def start(self, timeout_seconds: int = 30, model_service_type: str = "local") -> str:
        process = self.start_sandbox_service(model_service_type=model_service_type)
        pid = process.pid

        success = await self._wait_service_available(timeout_seconds)
        if not success:
            await self.stop(str(pid))
            raise Exception("Model service start failed")

        return str(pid)

    async def start_watch_agent(self, agent_pid: int):
        async with httpx.AsyncClient() as client:
            await client.post("http://127.0.0.1:8080/v1/agent/watch", json={"pid": agent_pid})

    async def stop(self, pid: str):
        subprocess.run(["kill", "-9", pid])

    async def _wait_service_available(self, timeout_seconds: int) -> bool:
        start = datetime.now()
        while (datetime.now() - start).seconds < timeout_seconds:
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get("http://127.0.0.1:8080/health")
                    if response.status_code == 200:
                        return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        return False
