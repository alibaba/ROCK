import asyncio
from contextlib import asynccontextmanager

from rock.actions import CreateBashSessionRequest
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

# =============================================================================
# Local Read/Write Separation Architecture Demo
# =============================================================================
#
# This script demonstrates how to use the proxy + admin read/write separation
# architecture locally:
#
#   - Admin server (port 8080): Handles write operations (start/stop sandbox)
#   - Proxy server (port 8081): Handles read/runtime operations (create session,
#     execute commands, etc.)
#
# Prerequisites:
#   1. Start Redis:
#      docker run -d --name redis-local -p 6379:6379 redis:latest
#   2. Start admin server:
#      rock admin start --env local-proxy --role admin --port 8080
#   3. Start proxy server:
#      rock admin start --env local-proxy --role proxy --port 8081
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
    base_url = "http://127.0.0.1:8080"
    proxy_url = "http://127.0.0.1:8081/apis/envs/sandbox/v1"
    admin_url = f"{base_url}/apis/envs/sandbox/v1"

    config = SandboxConfig(base_url=base_url, image=image, memory=memory, cpus=cpus)
    sandbox = Sandbox(config)

    try:
        # Write operation: create sandbox via admin server
        await sandbox.start()
        # Switch to proxy server for runtime operations
        sandbox.url = proxy_url
        yield sandbox
    finally:
        # Switch back to admin server for cleanup
        sandbox.url = admin_url
        await sandbox.stop()


async def run_sandbox():
    async with managed_sandbox() as sandbox:
        await sandbox.create_session(CreateBashSessionRequest(session="bash-1"))

        result = await sandbox.arun(cmd="echo Hello ROCK", session="bash-1")
        print(f"\n{'*' * 50}\n{result.output}\n{'*' * 50}\n")


if __name__ == "__main__":
    asyncio.run(run_sandbox())
