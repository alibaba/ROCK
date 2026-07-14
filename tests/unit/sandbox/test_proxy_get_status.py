"""
Unit tests for SandboxProxyService.get_status.

Covers:
  - meta_store miss → BadRequestRockError
  - RUNNING sandbox: rocklet RPCs populate phases/port_mapping/is_alive
  - PENDING → RUNNING transition writes start_time + meta_store.update
  - STOPPED + include_all_states=False → BadRequestRockError
  - STOPPED + include_all_states=True → response returned (is_alive=False)
  - rocklet RPC failure falls back to ServiceStatus() and is_alive=False
  - _rpc_client is reused (connection pool)
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.actions.sandbox.response import State
from rock.admin.proto.response import SandboxStatusResponse
from rock.sdk.common.exceptions import BadRequestRockError


def _make_meta_info(state: State = State.RUNNING, host_ip: str = "10.0.0.1") -> dict:
    return {
        "sandbox_id": "sandbox-1",
        "state": state,
        "host_ip": host_ip,
        "host_name": "node-1",
        "image": "python:3.11",
        "user_id": "u1",
        "experiment_id": "e1",
        "namespace": "ns1",
        "cpus": 1.0,
        "memory": "2g",
        "disk": "10g",
        "create_time": "2026-01-01T00:00:00",
        "state_history": [],
    }


def _make_rpc_execute_not_found():
    resp = MagicMock()
    resp.json.return_value = {"exit_code": 2, "output": ""}
    return resp


def _make_rpc_execute_success():
    resp = MagicMock()
    resp.json.return_value = {"exit_code": 0, "output": ""}
    return resp


def _make_rpc_read_file(content: str):
    resp = MagicMock()
    resp.json.return_value = {"content": content}
    return resp


def _make_rpc_is_alive(is_alive: bool):
    resp = MagicMock()
    resp.json.return_value = {"is_alive": is_alive, "message": "node-1"}
    return resp


SERVICE_STATUS_JSON = (
    '{"phases":{"image_pull":{"status":"success","message":"done"},'
    '"docker_run":{"status":"running","message":"running"}},'
    '"port_mapping":{"22555":22555,"8080":8080}}'
)


@pytest.fixture
def mock_meta_store():
    store = AsyncMock()
    store.get = AsyncMock(return_value=None)
    store.update = AsyncMock()
    store.get_timeout = AsyncMock(return_value=None)
    store.update_timeout = AsyncMock()
    return store


@pytest.fixture
def mock_rpc_client():
    return AsyncMock()


@pytest.fixture
def proxy_service(mock_meta_store, mock_rpc_client, rock_config):
    """Build a SandboxProxyService with mocked _meta_store and _rpc_client.

    Uses __new__ to skip __init__ (which constructs OSS clients, MetricsMonitor, etc.).
    """
    from rock.config import OssConfig, ProxyServiceConfig
    from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

    svc = SandboxProxyService.__new__(SandboxProxyService)
    svc._meta_store = mock_meta_store
    svc._rpc_client = mock_rpc_client
    svc._rock_config = rock_config
    svc.oss_config = OssConfig()
    svc.proxy_config = ProxyServiceConfig()
    svc._batch_get_status_max_count = 100
    svc._update_expire_time = AsyncMock()
    return svc


class TestProxyGetStatus:
    async def test_not_found_raises(self, proxy_service, mock_meta_store):
        mock_meta_store.get.return_value = None

        with pytest.raises(BadRequestRockError):
            await proxy_service.get_status("sandbox-1")

    async def test_running_sandbox_returns_phases_and_port_mapping(
        self, proxy_service, mock_meta_store, mock_rpc_client
    ):
        mock_meta_store.get.return_value = _make_meta_info(state=State.RUNNING)

        # Sequence: POST /execute (ls), POST /read_file, GET /is_alive
        mock_rpc_client.post.side_effect = [
            MagicMock(json=MagicMock(return_value={"exit_code": 0})),
            _make_rpc_read_file(SERVICE_STATUS_JSON),
        ]
        mock_rpc_client.get.return_value = _make_rpc_is_alive(True)

        result = await proxy_service.get_status("sandbox-1")

        assert isinstance(result, SandboxStatusResponse)
        assert result.state == State.RUNNING
        assert result.is_alive is True
        assert result.status == {
            "image_pull": {"status": "success", "message": "done"},
            "docker_run": {"status": "running", "message": "running"},
        }
        assert result.port_mapping == {22555: 22555, 8080: 8080}

    async def test_pending_to_running_triggers_meta_update(self, proxy_service, mock_meta_store, mock_rpc_client):
        mock_meta_store.get.return_value = _make_meta_info(state=State.PENDING, host_ip="10.0.0.1")
        mock_rpc_client.post.side_effect = [
            MagicMock(json=MagicMock(return_value={"exit_code": 0})),
            _make_rpc_read_file(SERVICE_STATUS_JSON),
        ]
        mock_rpc_client.get.return_value = _make_rpc_is_alive(True)

        result = await proxy_service.get_status("sandbox-1")

        assert result.state == State.RUNNING
        mock_meta_store.update.assert_awaited_once()
        updated_info = mock_meta_store.update.await_args[0][1]
        assert updated_info["state"] == State.RUNNING
        assert updated_info["start_time"] is not None

    async def test_stopped_with_include_all_states_false_raises(self, proxy_service, mock_meta_store):
        mock_meta_store.get.return_value = _make_meta_info(state=State.STOPPED)

        with pytest.raises(BadRequestRockError):
            await proxy_service.get_status("sandbox-1", include_all_states=False)

    async def test_stopped_with_include_all_states_true_returns_response(self, proxy_service, mock_meta_store):
        info = _make_meta_info(state=State.STOPPED)
        info.update(
            {
                "archive_time": "2026-01-01T00:00:00+00:00",
                "auto_transition_state": State.ARCHIVED,
                "auto_transition_time": "2026-01-01T01:00:00+00:00",
            }
        )
        mock_meta_store.get.return_value = info
        mock_meta_store.get_timeout.return_value = {"expire_time": "1767229200"}

        result = await proxy_service.get_status("sandbox-1", include_all_states=True)

        assert isinstance(result, SandboxStatusResponse)
        assert result.state == State.STOPPED
        assert result.is_alive is False
        assert result.archive_time == "2026-01-01T00:00:00+00:00"
        assert result.auto_stop_time is None
        assert result.auto_archive_time == "2026-01-01T01:00:00+00:00"
        assert result.auto_delete_time is None

    async def test_rocklet_failure_falls_back_gracefully(self, proxy_service, mock_meta_store, mock_rpc_client):
        mock_meta_store.get.return_value = _make_meta_info(state=State.RUNNING)
        mock_rpc_client.post.side_effect = Exception("connection refused")
        mock_rpc_client.get.side_effect = Exception("connection refused")

        result = await proxy_service.get_status("sandbox-1")

        assert isinstance(result, SandboxStatusResponse)
        assert result.is_alive is False
        # Falls back to meta_store snapshot (no phases stored there → None)
        assert result.status is None

    async def test_no_host_ip_skips_rocklet_calls(self, proxy_service, mock_meta_store, mock_rpc_client):
        mock_meta_store.get.return_value = _make_meta_info(state=State.PENDING, host_ip=None)

        result = await proxy_service.get_status("sandbox-1", include_all_states=True)

        assert result.is_alive is False
        mock_rpc_client.post.assert_not_awaited()
        mock_rpc_client.get.assert_not_awaited()

    async def test_uses_rpc_client_connection_pool(self, proxy_service, mock_rpc_client, mock_meta_store):
        """Ensure rocklet calls go through self._rpc_client (shared httpx pool),
        not per-request HttpUtils clients."""
        mock_meta_store.get.return_value = _make_meta_info(state=State.RUNNING)
        mock_rpc_client.post.side_effect = [
            _make_rpc_execute_success(),
            _make_rpc_read_file(SERVICE_STATUS_JSON),
        ]
        mock_rpc_client.get.return_value = _make_rpc_is_alive(True)

        await proxy_service.get_status("sandbox-1")

        # At least one post (execute/ls) and one get (is_alive) through the shared client
        assert mock_rpc_client.post.await_count >= 1
        assert mock_rpc_client.get.await_count >= 1
