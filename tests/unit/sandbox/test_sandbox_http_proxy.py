import asyncio
import json

import pytest
from starlette.datastructures import Headers

from rock.admin.proto.request import (
    SandboxBashAction as BashAction,
)
from rock.admin.proto.request import (
    SandboxCloseBashSessionRequest as CloseBashSessionRequest,
)
from rock.admin.proto.request import (
    SandboxCreateBashSessionRequest as CreateSessionRequest,
)
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from tests.unit.conftest import check_sandbox_status_until_alive

logger = init_logger(__name__)

ECHO_SERVER_SCRIPT = r"""
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        data = json.loads(body)
        response = {"path": self.path, "echo": data}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""

SESSION_NAME = "test"


async def start_echo_server_in_sandbox(
    sandbox_proxy_service: SandboxProxyService,
    sandbox_id: str,
) -> None:
    """Start an echo HTTP server inside the sandbox container via create_session + run_in_session."""

    create_req = CreateSessionRequest(
        session=SESSION_NAME,
        sandbox_id=sandbox_id,
    )
    await sandbox_proxy_service.create_session(create_req)

    # Write echo server script to file
    write_action = BashAction(
        action_type="bash",
        sandbox_id=sandbox_id,
        session=SESSION_NAME,
        command="cat > /tmp/echo_server.py << 'PYEOF'\n" + ECHO_SERVER_SCRIPT.strip() + "\nPYEOF",
    )
    await sandbox_proxy_service.run_in_session(write_action)

    # Start echo server in background
    start_action = BashAction(
        action_type="bash",
        sandbox_id=sandbox_id,
        session=SESSION_NAME,
        command="nohup python3 /tmp/echo_server.py > /tmp/server.log 2>&1 & echo $!",
    )
    start_result = await sandbox_proxy_service.run_in_session(start_action)
    logger.info(f"echo server started, result: {start_result}")

    # Wait for server to be ready
    await asyncio.sleep(2)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_post_proxy(sandbox_manager: SandboxManager, sandbox_proxy_service: SandboxProxyService):
    response = await sandbox_manager.start_async(DockerDeploymentConfig(cpus=0.5, memory="1g"))
    sandbox_id = response.sandbox_id
    await check_sandbox_status_until_alive(sandbox_manager, sandbox_id)

    try:
        await start_echo_server_in_sandbox(sandbox_proxy_service, sandbox_id)

        mock_headers = Headers({"content-type": "application/json"})

        # Test with path and body
        result = await sandbox_proxy_service.post_proxy(
            sandbox_id=sandbox_id,
            target_path="api/test",
            body={"hello": "world"},
            headers=mock_headers,
        )
        assert result.status_code == 200
        response_body = json.loads(result.body)
        assert response_body["path"] == "/api/test"
        assert response_body["echo"] == {"hello": "world"}

        # Test without path
        result = await sandbox_proxy_service.post_proxy(
            sandbox_id=sandbox_id,
            target_path="",
            body={"key": "value"},
            headers=mock_headers,
        )
        assert result.status_code == 200
        response_body = json.loads(result.body)
        assert response_body["echo"] == {"key": "value"}

        # Test with body as None
        result = await sandbox_proxy_service.post_proxy(
            sandbox_id=sandbox_id,
            target_path="health",
            body=None,
            headers=mock_headers,
        )
        assert result.status_code == 200
        response_body = json.loads(result.body)
        assert response_body["echo"] == {}
        assert response_body["path"] == "/health"

    finally:
        try:
            close_req = CloseBashSessionRequest(
                session=SESSION_NAME,
                sandbox_id=sandbox_id,
            )
            await sandbox_proxy_service.close_session(close_req)
        except Exception:
            pass
        await sandbox_manager.stop(sandbox_id)
