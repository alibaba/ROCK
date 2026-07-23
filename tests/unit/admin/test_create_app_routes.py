import socket

import pytest
import uvicorn
from fastapi import FastAPI

from rock.admin.main import _create_reuse_port_socket, _include_routers


def _paths(app: FastAPI) -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


def test_proxy_role_mounts_proxy_router():
    app = FastAPI()
    _include_routers(app, role="proxy")
    paths = _paths(app)
    commit_path = "/apis/envs/sandbox/v1/commit"
    commit_status_path = "/apis/envs/sandbox/v1/commit/{sandbox_id}"
    assert any(p.endswith("/get_token") for p in paths)
    assert commit_path in paths
    assert commit_status_path in paths


def test_proxy_role_excludes_admin_router():
    app = FastAPI()
    _include_routers(app, role="proxy")
    paths = _paths(app)
    assert not any("/ops" in p for p in paths)


def test_admin_role_mounts_admin_routers():
    app = FastAPI()
    _include_routers(app, role="admin")
    paths = _paths(app)
    commit_path = "/apis/envs/sandbox/v1/commit"
    commit_status_path = "/apis/envs/sandbox/v1/commit/{sandbox_id}"
    assert any("/ops" in p for p in paths)
    assert commit_path in paths
    assert commit_status_path not in paths


@pytest.mark.skipif(not hasattr(socket, "SO_REUSEPORT"), reason="SO_REUSEPORT unavailable")
def test_create_reuse_port_socket_enables_reuseport():
    # port=0 -> kernel picks a free ephemeral port, so the test never conflicts.
    config = uvicorn.Config("rock.admin.main:create_app", factory=True, host="127.0.0.1", port=0)
    listener = _create_reuse_port_socket(config)
    try:
        assert listener.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) == 1
        assert listener.getsockname()[0] == "127.0.0.1"
    finally:
        listener.close()
