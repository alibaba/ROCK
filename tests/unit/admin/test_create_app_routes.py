from fastapi import FastAPI

from rock.admin.main import _include_routers


def _paths(app: FastAPI) -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


def test_proxy_role_mounts_proxy_router():
    app = FastAPI()
    _include_routers(app, role="proxy")
    paths = _paths(app)
    assert any(p.endswith("/get_token") for p in paths)


def test_proxy_role_excludes_admin_router():
    app = FastAPI()
    _include_routers(app, role="proxy")
    paths = _paths(app)
    assert not any("/ops" in p for p in paths)


def test_admin_role_mounts_admin_routers():
    app = FastAPI()
    _include_routers(app, role="admin")
    paths = _paths(app)
    assert any("/ops" in p for p in paths)
