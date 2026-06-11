"""Unit tests for Sandbox.attach() and sandbox_id lifecycle."""

from unittest.mock import AsyncMock, patch

import pytest

from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

SANDBOX_ID = "my-sandbox-123"


class TestSandboxClientInit:
    def test_sandbox_id_none_by_default(self):
        config = SandboxConfig()
        sandbox = Sandbox(config)
        assert sandbox.sandbox_id is None

    def test_config_sandbox_id_not_propagated(self):
        config = SandboxConfig(sandbox_id=SANDBOX_ID)
        sandbox = Sandbox(config)
        assert sandbox.sandbox_id is None


class TestSandboxAttach:
    def _make_sandbox(self) -> Sandbox:
        return Sandbox(SandboxConfig())

    def _mock_status(self, **overrides) -> AsyncMock:
        defaults = {
            "sandbox_id": SANDBOX_ID,
            "host_name": "host-1",
            "host_ip": "10.0.0.1",
            "namespace": None,
            "experiment_id": None,
            "image": None,
            "cpus": None,
            "memory": None,
            "user_id": None,
        }
        defaults.update(overrides)
        mock = AsyncMock()
        for k, v in defaults.items():
            setattr(mock, k, v)
        return mock

    @pytest.mark.asyncio
    async def test_attach_sets_sandbox_info(self):
        sandbox = self._make_sandbox()
        mock_status = self._mock_status(
            namespace="ns-1",
            experiment_id="exp-1",
            image="ubuntu:22.04",
            cpus=4.0,
            memory="16g",
            user_id="user-1",
        )

        with patch.object(sandbox, "get_status", return_value=mock_status) as mock_get:
            await sandbox.attach(SANDBOX_ID)
            mock_get.assert_called_once_with(include_all_states=True)

        assert sandbox.sandbox_id == SANDBOX_ID
        assert sandbox.host_name == "host-1"
        assert sandbox.host_ip == "10.0.0.1"
        assert sandbox._namespace == "ns-1"
        assert sandbox._experiment_id == "exp-1"

        assert sandbox.config.sandbox_id == SANDBOX_ID
        assert sandbox.config.image == "ubuntu:22.04"
        assert sandbox.config.cpus == 4.0
        assert sandbox.config.memory == "16g"
        assert sandbox.config.user_id == "user-1"
        assert sandbox.config.experiment_id == "exp-1"
        assert sandbox.config.namespace == "ns-1"

    @pytest.mark.asyncio
    async def test_attach_raises_on_get_status_failure(self):
        sandbox = self._make_sandbox()

        with patch.object(sandbox, "get_status", side_effect=Exception("Failed to get status")):
            with pytest.raises(Exception, match=f"Failed to attach sandbox {SANDBOX_ID}"):
                await sandbox.attach(SANDBOX_ID)

        assert sandbox.sandbox_id is None

    @pytest.mark.asyncio
    async def test_attach_raises_on_sandbox_id_mismatch(self):
        sandbox = self._make_sandbox()
        mock_status = self._mock_status(sandbox_id="different-id")

        with patch.object(sandbox, "get_status", return_value=mock_status):
            with pytest.raises(Exception, match="sandbox_id mismatch"):
                await sandbox.attach(SANDBOX_ID)

        assert sandbox.sandbox_id is None

    @pytest.mark.asyncio
    async def test_attach_syncs_none_from_server(self):
        sandbox = self._make_sandbox()
        mock_status = self._mock_status()

        with patch.object(sandbox, "get_status", return_value=mock_status):
            await sandbox.attach(SANDBOX_ID)

        assert sandbox._namespace is None
        assert sandbox._experiment_id is None
        assert sandbox.config.image is None
        assert sandbox.config.cpus is None
        assert sandbox.config.memory is None

    @pytest.mark.asyncio
    async def test_attach_then_restart(self):
        sandbox = self._make_sandbox()

        with patch.object(sandbox, "get_status", return_value=self._mock_status()):
            await sandbox.attach(SANDBOX_ID)

        restart_response = {"status": "Success"}
        restart_status = AsyncMock()
        restart_status.is_alive = True

        with (
            patch.object(sandbox, "_build_headers", return_value={}),
            patch("rock.utils.HttpUtils.post", new_callable=AsyncMock, return_value=restart_response),
            patch.object(sandbox, "get_status", return_value=restart_status),
        ):
            await sandbox.restart()

        assert sandbox.sandbox_id == SANDBOX_ID
