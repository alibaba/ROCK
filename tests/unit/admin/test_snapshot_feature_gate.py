from fastapi import FastAPI

from rock.admin.main import _include_routers


def _paths(app: FastAPI) -> set[str]:
    return set(app.openapi()["paths"])


def test_snapshot_routes_are_not_hidden_by_the_retired_feature_gate(monkeypatch):
    monkeypatch.delenv("ROCK_SNAPSHOT_API_ENABLED", raising=False)
    app = FastAPI()
    _include_routers(app, role="admin")
    paths = _paths(app)
    assert "/apis/envs/sandbox/v1/start_async" in paths
    assert "/apis/envs/sandbox/v1/snapshots" in paths
    assert "/apis/envs/sandbox/v1/snapshots/base-images" in paths


def test_obsolete_false_value_cannot_restore_the_old_snapshot_route_set(monkeypatch):
    monkeypatch.setenv("ROCK_SNAPSHOT_API_ENABLED", "false")
    app = FastAPI()
    _include_routers(app, role="admin")
    paths = _paths(app)
    assert "/apis/envs/sandbox/v1/start_async" in paths
    assert "/apis/envs/sandbox/v1/snapshots" in paths
    assert "/apis/envs/sandbox/v1/snapshots/base-images" in paths
