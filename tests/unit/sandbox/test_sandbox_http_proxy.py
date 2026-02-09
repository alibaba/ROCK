import asyncio
import json
import textwrap

import pytest
from starlette.datastructures import Headers

from rock.admin.proto.request import SandboxCommand as Command
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from tests.unit.conftest import check_sandbox_status_until_alive

ECHO_SERVER_SCRIPT = textwrap.dedent(
    """\
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
)


async def start_echo_server(sandbox_proxy_service: SandboxProxyService, sandbox_id: str):
    """在 sandbox 容器内启动一个 echo HTTP 服务（监听 8080）"""
    # 写文件
    write_cmd = Command(
        sandbox_id=sandbox_id,
        command=f"cat > /tmp/echo_server.py << 'PYEOF'\n{ECHO_SERVER_SCRIPT}\nPYEOF",
    )
    await sandbox_proxy_service.execute(write_cmd)

    # 后台启动
    start_cmd = Command(
        sandbox_id=sandbox_id,
        command="nohup python3 /tmp/echo_server.py > /dev/null 2>&1 &",
    )
    await sandbox_proxy_service.execute(start_cmd)

    # 等待服务就绪
    await asyncio.sleep(2)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_post_proxy(sandbox_manager: SandboxManager, sandbox_proxy_service: SandboxProxyService):
    response = await sandbox_manager.start_async(DockerDeploymentConfig(cpus=0.5, memory="1g"))
    sandbox_id = response.sandbox_id
    await check_sandbox_status_until_alive(sandbox_manager, sandbox_id)

    try:
        await start_echo_server(sandbox_proxy_service, sandbox_id)

        mock_headers = Headers({"content-type": "application/json"})

        # 测试有 path + body
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

        # 测试无 path
        result = await sandbox_proxy_service.post_proxy(
            sandbox_id=sandbox_id,
            target_path="",
            body={"key": "value"},
            headers=mock_headers,
        )
        assert result.status_code == 200
        response_body = json.loads(result.body)
        assert response_body["echo"] == {"key": "value"}

        # 测试 body 为 None（会被转为 {}）
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
        await sandbox_manager.stop(sandbox_id)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_post_proxy_invalid_sandbox(sandbox_proxy_service: SandboxProxyService):
    """无效 sandbox_id 应抛出异常"""
    mock_headers = Headers({"content-type": "application/json"})

    with pytest.raises(Exception):
        await sandbox_proxy_service.post_proxy(
            sandbox_id="invalid_sandbox_id",
            target_path="",
            body={},
            headers=mock_headers,
        )
