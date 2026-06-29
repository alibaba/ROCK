"""Unit tests for FCOperator lifecycle.

Verifies FCOperator lifecycle:
- submit creates function and session via InvokeFunction
- C7: submit() returns memory as int, crashes SandboxManager.convert_to_gb
- rollback: session failure triggers function cleanup
- coverage: submit/get_status/stop/_create_function are exercised
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.actions.sandbox.response import State
from rock.common.constants import StopReason
from rock.sandbox.operator.fc.config import FCOperatorConfig
from rock.sandbox.operator.fc.operator import FCOperator


@pytest.fixture
def operator(fc_config):
    return FCOperator(fc_config)


class TestSubmit:
    async def test_submit_requires_image(self, operator, fc_operator_config):
        config = FCOperatorConfig(image=None)
        with pytest.raises(RuntimeError, match="image is required"):
            await operator.submit(config, {})

    async def test_submit_creates_function_and_session(self, operator, fc_operator_config, monkeypatch):
        monkeypatch.setattr(operator, "_get_or_create_function", AsyncMock(return_value="rock-sandbox-func"))
        mock_runtime = MagicMock()
        mock_runtime.create_session = AsyncMock(return_value=MagicMock(output="root@fc:~$ "))
        monkeypatch.setattr("rock.sandbox.operator.fc.operator.FCRuntime", MagicMock(return_value=mock_runtime))

        info = await operator.submit(fc_operator_config, {"user_id": "u1"})

        assert info["sandbox_id"] == fc_operator_config.session_id
        assert info["type"] == "fc"
        assert info["function_name"] == "rock-sandbox-func"
        assert info["state"] == State.RUNNING
        assert operator._sandbox_functions[fc_operator_config.session_id] == "rock-sandbox-func"
        mock_runtime.create_session.assert_awaited_once()

    async def test_submit_returns_memory_as_string(self, operator, fc_operator_config, monkeypatch):
        monkeypatch.setattr(operator, "_get_or_create_function", AsyncMock(return_value="rock-sandbox-func"))
        mock_runtime = MagicMock()
        mock_runtime.create_session = AsyncMock(return_value=MagicMock(output="root@fc:~$ "))
        monkeypatch.setattr("rock.sandbox.operator.fc.operator.FCRuntime", MagicMock(return_value=mock_runtime))

        info = await operator.submit(fc_operator_config, {})

        assert isinstance(info["memory"], str)
        assert info["memory"].endswith("m")

    async def test_submit_does_not_delete_function_on_session_failure(self, operator, fc_operator_config, monkeypatch):
        """Session failure should NOT delete function (template may be shared)."""
        monkeypatch.setattr(operator, "_get_or_create_function", AsyncMock(return_value="rock-sandbox-func"))
        delete_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(operator, "_delete_function", delete_mock)
        mock_runtime = MagicMock()
        mock_runtime.create_session = AsyncMock(side_effect=RuntimeError("session failed"))
        monkeypatch.setattr("rock.sandbox.operator.fc.operator.FCRuntime", MagicMock(return_value=mock_runtime))

        with pytest.raises(RuntimeError, match="session failed"):
            await operator.submit(fc_operator_config, {})

        # Function should NOT be deleted (template reuse design)
        delete_mock.assert_not_awaited()


class TestGetStatus:
    async def test_returns_running_when_alive(self, operator, fc_operator_config):
        sid = fc_operator_config.session_id
        mock_runtime = MagicMock()
        mock_runtime.is_alive = AsyncMock(return_value=MagicMock(is_alive=True))
        operator._runtimes[sid] = mock_runtime
        operator._runtime_configs[sid] = fc_operator_config

        info = await operator.get_status(sid)

        assert info["state"] == State.RUNNING
        assert info["sandbox_id"] == sid

    async def test_returns_none_when_not_found(self, operator):
        """W5: get_status should return None (not raise) for unknown sandbox."""
        info = await operator.get_status("missing-sid")
        assert info is None


class TestStop:
    async def test_stop_closes_runtime_and_deletes_function(self, operator, fc_operator_config):
        sid = fc_operator_config.session_id
        mock_runtime = MagicMock()
        mock_runtime.close = AsyncMock()
        operator._runtimes[sid] = mock_runtime
        operator._sandbox_functions[sid] = "rock-sandbox-func"
        delete_mock = AsyncMock(return_value=True)
        operator._delete_function = delete_mock  # type: ignore[method-assign]

        result = await operator.stop(sid)

        assert result is True
        mock_runtime.close.assert_awaited_once()
        delete_mock.assert_awaited_once_with("rock-sandbox-func")
        assert sid not in operator._runtimes

    async def test_stop_untracked_returns_true(self, operator):
        assert await operator.stop("unknown") is True


class TestRestart:
    async def test_restart_stops_then_submits(self, operator, fc_operator_config, monkeypatch):
        """restart() should stop the old session and submit a new one."""
        stop_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(operator, "stop", stop_mock)
        monkeypatch.setattr(operator, "_get_or_create_function", AsyncMock(return_value="rock-sandbox-func"))
        mock_runtime = MagicMock()
        mock_runtime.create_session = AsyncMock(return_value=MagicMock(output="root@fc:~$ "))
        monkeypatch.setattr("rock.sandbox.operator.fc.operator.FCRuntime", MagicMock(return_value=mock_runtime))

        info = await operator.restart(fc_operator_config)

        stop_mock.assert_awaited_once_with(fc_operator_config.session_id, reason=StopReason.MANUAL)
        assert info["sandbox_id"] == fc_operator_config.session_id
        assert info["state"] == State.RUNNING

    async def test_restart_rejects_non_fc_config(self, operator, fc_operator_config):
        """W6: restart() should reject non-FCOperatorConfig."""
        from rock.deployments.config import DockerDeploymentConfig

        docker_config = DockerDeploymentConfig(image="test", memory="4g", cpus=2)
        docker_config.container_name = "docker-sid"
        with pytest.raises(ValueError, match="Cannot restart FC sandbox with config type"):
            await operator.restart(docker_config)


class TestDelete:
    async def test_delete_delegates_to_stop(self, operator, fc_operator_config, monkeypatch):
        """delete() should delegate to stop()."""
        stop_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(operator, "stop", stop_mock)

        result = await operator.delete(fc_operator_config)

        assert result is True
        stop_mock.assert_awaited_once_with(fc_operator_config.session_id, reason=StopReason.MANUAL)


class TestCreateFunction:
    async def test_create_function_builds_request_with_config(self, operator, fc_operator_config, fake_fc_sdk):
        response = MagicMock()
        response.body.function_name = "rock-sandbox-testsession01"
        operator._fc_client = MagicMock()
        operator._fc_client.create_function_with_options = MagicMock(return_value=response)

        name = await operator._create_function(fc_operator_config, "rock-sandbox-test")

        assert name == "rock-sandbox-test"
        operator._fc_client.create_function_with_options.assert_called_once()
        call_args = operator._fc_client.create_function_with_options.call_args.args
        request = call_args[0]
        body = request.body
        assert body.memory_size == fc_operator_config.memory
        assert body.cpu == fc_operator_config.cpus
        assert body.custom_container_config.image == fc_operator_config.image
        assert body.runtime == "custom-container"

    async def test_delete_function_returns_bool(self, operator, fake_fc_sdk):
        operator._fc_client = MagicMock()
        operator._fc_client.delete_function_with_options = MagicMock(return_value=MagicMock())
        assert await operator._delete_function("fn") is True

        operator._fc_client.delete_function_with_options = MagicMock(side_effect=Exception("boom"))
        assert await operator._delete_function("fn") is False
