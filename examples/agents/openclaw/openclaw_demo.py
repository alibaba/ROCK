import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

# =============================================================================
# Local Read/Write Separation Architecture Demo
# =============================================================================
#
# This script demonstrates how to use the proxy + admin read/write separation
# architecture locally:
#
#   - Admin server (port 9000): Handles write operations (start/stop sandbox)
#   - Proxy server (port 9001): Handles read/runtime operations (create session,
#     execute commands, etc.)
#
# Prerequisites:
#   1. Start Redis:
#      docker run -d --name redis-local -p 6379:6379 redis:latest
#   2. Start admin server:
#      rock admin start --env local-proxy --role admin --port 9000
#   3. Start proxy server:
#      rock admin start --env local-proxy --role proxy --port 9001
#
# NOTE: For production environments, it is recommended to add a gateway layer
#       (e.g. Nginx, Kong, or a custom API gateway) to route requests
#       transparently, so that clients don't need to manage URL switching
#       between admin and proxy manually.
# =============================================================================


@asynccontextmanager
async def managed_sandbox(
    image: str = "python:3.11",
    memory: str = "2g",
    cpus: float = 0.5,
):
    """Context manager that handles sandbox lifecycle with read/write separation.

    Lifecycle:
        1. start()  -> routed to admin server  (write operation)
        2. yield     -> url switched to proxy   (read/runtime operations)
        3. stop()   -> url restored to admin    (write operation)

    Args:
        image: Docker image to use for the sandbox.
        memory: Memory limit for the sandbox container.
        cpus: CPU limit for the sandbox container.
    """
    base_url = "http://127.0.0.1:9000"
    proxy_url = "http://127.0.0.1:9001/apis/envs/sandbox/v1"

    config = SandboxConfig(base_url=base_url, image=image, memory=memory, cpus=cpus)
    sandbox = Sandbox(config)

    # Write operation: create sandbox via admin server
    await sandbox.start()
    # Switch to proxy server for runtime operations
    sandbox.url = proxy_url
    yield sandbox


async def run_sandbox():
    async with managed_sandbox() as sandbox:
        await sandbox.agent.install()
        await sandbox.agent.run("")

        print(
            f"╔══════════════════════════════════════╗\n"
            f"║  🟢 Sandbox ID: {sandbox.sandbox_id:<20s}║\n"
            f"╚══════════════════════════════════════╝"
        )


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)

    asyncio.run(run_sandbox())
