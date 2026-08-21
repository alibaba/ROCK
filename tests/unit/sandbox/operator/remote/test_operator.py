"""Unit tests for RemoteOperator — provider delegation + Redis merge."""

import pytest
from unittest.mock import AsyncMock

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.common.constants import StopReason
from rock.config import RemoteOperatorConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.remote.constants import EXT_REMOTE_ID, EXT_BACKEND, BACKEND_NAME
from rock.sandbox.operator.remote.operator import RemoteOperator


def _make_config(**overrides) -> RemoteOperatorConfig:
    defaults = {"endpoint": "api.sandbox.test", "api_key": "test-key"}
    defaults.update(overrides)
    return RemoteOperatorConfig(**defaults)


def _make_docker_config(**overrides) -> DockerDeploymentConfig:
    defaults = {
        "image": "python:3.11",
        "cpus": 2.0,
        "memory": "8g",
        "disk": "50G",
        "container_name": "sb-test-001",
    }
    defaults.update(overrides)
    return DockerDeploymentConfig(**defaults)


class TestRemoteOperatorInit:
    def test_default_provider_is_sandbox_next(self):
        op = RemoteOperator(_make_config())
        from rock.sandbox.operator.remote.providers.sandbox_next_provider import SandboxNextProvider

        assert isinstance(op._provider, SandboxNextProvider)

    def test_unsupported_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported remote provider"):
            RemoteOperator(_make_config(provider="e2b"))

    def test_missing_endpoint_raises(self):
        with pytest.raises(ValueError, match="endpoint is required"):
            RemoteOperatorConfig(endpoint="")


class TestRemoteOperatorSubmit:
    @pytest.mark.asyncio
    async def test_submit_delegates_to_provider(self):
        op = RemoteOperator(_make_config())
        op._provider = AsyncMock()
        expected_info: SandboxInfo = {"sandbox_id": "sb-1", "state": State.PENDING}
        op._provider.submit = AsyncMock(return_value=expected_info)

        docker_config = _make_docker_config()
        result = await op.submit(docker_config, {"user_id": "u1"})
        assert result == expected_info
        op._provider.submit.assert_awaited_once()


class TestRemoteOperatorGetStatus:
    @pytest.mark.asyncio
    async def test_no_redis_info_returns_none(self):
        op = RemoteOperator(_make_config())
        op._redis_provider = None
        # get_sandbox_info_from_redis will raise RuntimeError without provider
        # but the method checks redis_info first; mock it
        op.get_sandbox_info_from_redis = AsyncMock(return_value=None)
        result = await op.get_status("sb-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_remote_id_returns_none(self):
        op = RemoteOperator(_make_config())
        op.get_sandbox_info_from_redis = AsyncMock(return_value={"sandbox_id": "sb-1"})
        result = await op.get_status("sb-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_merge_redis_and_provider_info(self):
        op = RemoteOperator(_make_config())
        op._provider = AsyncMock()
        op._provider.get_status = AsyncMock(return_value={
            "sandbox_id": "sb-1",
            "state": State.RUNNING,
            "host_ip": "host.example.com",
            "port_mapping": {22555: 443},
            "auth_token": "tok",
            "extended_params": {EXT_REMOTE_ID: "sn-1", EXT_BACKEND: BACKEND_NAME},
        })
        op.get_sandbox_info_from_redis = AsyncMock(return_value={
            "sandbox_id": "sb-1",
            "state": State.PENDING,
            "user_id": "u1",
            "image": "python:3.11",
            "extended_params": {EXT_REMOTE_ID: "sn-1", "custom_key": "val"},
        })
        result = await op.get_status("sb-1")
        assert result is not None
        assert result["state"] == State.RUNNING
        assert result["user_id"] == "u1"
        assert result["host_ip"] == "host.example.com"
        ext = result["extended_params"]
        assert ext[EXT_REMOTE_ID] == "sn-1"
        assert ext["custom_key"] == "val"  # preserved from Redis
        assert ext[EXT_BACKEND] == BACKEND_NAME  # from provider

    @pytest.mark.asyncio
    async def test_provider_404_marks_deleted(self):
        op = RemoteOperator(_make_config())
        op._provider = AsyncMock()
        op._provider.get_status = AsyncMock(return_value=None)
        op.get_sandbox_info_from_redis = AsyncMock(return_value={
            "sandbox_id": "sb-1",
            "state": State.RUNNING,
            "extended_params": {EXT_REMOTE_ID: "sn-1"},
        })
        result = await op.get_status("sb-1")
        assert result is not None
        assert result["state"] == "deleted"


class TestRemoteOperatorStop:
    @pytest.mark.asyncio
    async def test_stop_delegates_to_provider(self):
        op = RemoteOperator(_make_config())
        op._provider = AsyncMock()
        op._provider.stop = AsyncMock(return_value=True)
        op.get_sandbox_info_from_redis = AsyncMock(return_value={
            "sandbox_id": "sb-1",
            "extended_params": {EXT_REMOTE_ID: "sn-1"},
        })
        result = await op.stop("sb-1", StopReason.MANUAL)
        assert result is True
        op._provider.stop.assert_awaited_once_with("sn-1")

    @pytest.mark.asyncio
    async def test_stop_no_remote_id_raises(self):
        op = RemoteOperator(_make_config())
        op.get_sandbox_info_from_redis = AsyncMock(return_value=None)
        with pytest.raises(Exception, match="cannot resolve"):
            await op.stop("sb-1")


class TestRemoteOperatorDelete:
    @pytest.mark.asyncio
    async def test_delete_from_config_extended_params(self):
        op = RemoteOperator(_make_config())
        op._provider = AsyncMock()
        op._provider.delete = AsyncMock(return_value=True)
        docker_config = _make_docker_config()
        docker_config.extended_params = {EXT_REMOTE_ID: "sn-1"}
        result = await op.delete(docker_config)
        assert result is True
        op._provider.delete.assert_awaited_once_with("sn-1")

    @pytest.mark.asyncio
    async def test_delete_fallback_to_redis(self):
        op = RemoteOperator(_make_config())
        op._provider = AsyncMock()
        op._provider.delete = AsyncMock(return_value=True)
        op.get_sandbox_info_from_redis = AsyncMock(return_value={
            "sandbox_id": "sb-1",
            "extended_params": {EXT_REMOTE_ID: "sn-redis"},
        })
        docker_config = _make_docker_config()
        result = await op.delete(docker_config)
        assert result is True
        op._provider.delete.assert_awaited_once_with("sn-redis")


class TestRemoteOperatorRestart:
    @pytest.mark.asyncio
    async def test_restart_not_supported(self):
        from rock.sdk.common.exceptions import BadRequestRockError

        op = RemoteOperator(_make_config())
        with pytest.raises(BadRequestRockError, match="restart"):
            await op.restart(_make_docker_config())


class TestRemoteOperatorTemplateAPI:
    @pytest.mark.asyncio
    async def test_create_template_delegates(self):
        op = RemoteOperator(_make_config())
        op._provider = AsyncMock()
        op._provider.create_template = AsyncMock(return_value={"template_id": "tpl-1", "status": "pending"})
        result = await op.create_template({"name": "test"})
        assert result["template_id"] == "tpl-1"

    @pytest.mark.asyncio
    async def test_create_template_not_implemented_fallback(self):
        from rock.sdk.common.exceptions import BadRequestRockError

        op = RemoteOperator(_make_config())
        op._provider = AsyncMock()
        op._provider.create_template = AsyncMock(side_effect=NotImplementedError())
        with pytest.raises(BadRequestRockError, match="template not supported"):
            await op.create_template({"name": "test"})

    @pytest.mark.asyncio
    async def test_get_template_status_404(self):
        op = RemoteOperator(_make_config())
        op._provider = AsyncMock()
        op._provider.get_template_status = AsyncMock(return_value=None)
        result = await op.get_template_status("tpl-gone")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_template_success(self):
        op = RemoteOperator(_make_config())
        op._provider = AsyncMock()
        op._provider.delete_template = AsyncMock(return_value=True)
        assert await op.delete_template("tpl-1") is True
