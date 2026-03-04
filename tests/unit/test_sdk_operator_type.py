"""Unit tests for SDK operator_type support.

Covers:
- SandboxConfig.operator_type field (default, explicit value)
- Sandbox.__init__ stores operator_type from config
- Sandbox.start() includes operator_type in the request payload
- SandboxGroup propagates operator_type to all child sandboxes
"""

from unittest.mock import AsyncMock, patch

import pytest

from rock.sdk.sandbox.client import Sandbox, SandboxGroup
from rock.sdk.sandbox.config import SandboxConfig, SandboxGroupConfig

# ===========================================================================
# 1. SandboxConfig operator_type field
# ===========================================================================


class TestSandboxConfigOperatorType:
    """Test SandboxConfig.operator_type field behavior."""

    def test_operator_type_default_is_ray(self):
        """operator_type should default to 'ray' when not specified."""
        config = SandboxConfig()
        assert config.operator_type == "ray"

    def test_operator_type_set_explicitly(self):
        """operator_type should be stored when explicitly set."""
        config = SandboxConfig(operator_type="k8s")
        assert config.operator_type == "k8s"

    def test_operator_type_ray(self):
        """operator_type should accept 'ray' value."""
        config = SandboxConfig(operator_type="ray")
        assert config.operator_type == "ray"

    def test_operator_type_with_other_fields(self):
        """operator_type should coexist with other config fields."""
        config = SandboxConfig(
            image="ubuntu:22.04",
            memory="16g",
            cpus=4,
            cluster="us-east",
            operator_type="k8s",
        )
        assert config.operator_type == "k8s"
        assert config.image == "ubuntu:22.04"
        assert config.memory == "16g"
        assert config.cpus == 4
        assert config.cluster == "us-east"

    def test_operator_type_serialization(self):
        """operator_type should appear in model_dump output."""
        config = SandboxConfig(operator_type="ray")
        dumped = config.model_dump()
        assert "operator_type" in dumped
        assert dumped["operator_type"] == "ray"

    def test_operator_type_default_serialization(self):
        """Default operator_type='ray' should appear in model_dump output."""
        config = SandboxConfig()
        dumped = config.model_dump()
        assert "operator_type" in dumped
        assert dumped["operator_type"] == "ray"


# ===========================================================================
# 2. Sandbox.__init__ with operator_type
# ===========================================================================


class TestSandboxInitOperatorType:
    """Test that Sandbox stores operator_type from SandboxConfig."""

    def test_sandbox_stores_operator_type_from_config(self):
        """Sandbox should store the config with operator_type."""
        config = SandboxConfig(operator_type="k8s")
        sandbox = Sandbox(config)
        assert sandbox.config.operator_type == "k8s"

    def test_sandbox_stores_default_operator_type(self):
        """Sandbox should store default operator_type='ray' when not specified."""
        config = SandboxConfig()
        sandbox = Sandbox(config)
        assert sandbox.config.operator_type == "ray"


# ===========================================================================
# 3. Sandbox.start() includes operator_type in request
# ===========================================================================


class TestSandboxStartOperatorType:
    """Test that Sandbox.start() sends operator_type in the POST payload."""

    @pytest.mark.asyncio
    async def test_start_sends_operator_type_in_payload(self):
        """start() should include operator_type in the request data."""
        config = SandboxConfig(operator_type="k8s", startup_timeout=5)
        sandbox = Sandbox(config)

        mock_response = {
            "status": "Success",
            "result": {
                "sandbox_id": "test-sandbox-001",
                "host_name": "test-host",
                "host_ip": "10.0.0.1",
            },
        }

        # Mock get_status to return alive immediately
        mock_status = AsyncMock()
        mock_status.is_alive = True

        with patch("rock.utils.http.HttpUtils.post", new_callable=AsyncMock) as mock_post, patch.object(
            sandbox, "get_status", return_value=mock_status
        ):
            mock_post.return_value = mock_response
            await sandbox.start()

            # Verify the POST was called with operator_type in data
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            posted_data = call_args[0][2]  # third positional arg is data
            assert "operator_type" in posted_data
            assert posted_data["operator_type"] == "k8s"

    @pytest.mark.asyncio
    async def test_start_sends_default_operator_type_when_not_set(self):
        """start() should send operator_type='ray' when using default config."""
        config = SandboxConfig(startup_timeout=5)
        sandbox = Sandbox(config)

        mock_response = {
            "status": "Success",
            "result": {
                "sandbox_id": "test-sandbox-002",
                "host_name": "test-host",
                "host_ip": "10.0.0.1",
            },
        }

        mock_status = AsyncMock()
        mock_status.is_alive = True

        with patch("rock.utils.http.HttpUtils.post", new_callable=AsyncMock) as mock_post, patch.object(
            sandbox, "get_status", return_value=mock_status
        ):
            mock_post.return_value = mock_response
            await sandbox.start()

            call_args = mock_post.call_args
            posted_data = call_args[0][2]
            assert "operator_type" in posted_data
            assert posted_data["operator_type"] == "ray"

    @pytest.mark.asyncio
    async def test_start_sends_ray_operator_type(self):
        """start() should correctly send operator_type='ray'."""
        config = SandboxConfig(operator_type="ray", startup_timeout=5)
        sandbox = Sandbox(config)

        mock_response = {
            "status": "Success",
            "result": {
                "sandbox_id": "test-sandbox-003",
                "host_name": "test-host",
                "host_ip": "10.0.0.1",
            },
        }

        mock_status = AsyncMock()
        mock_status.is_alive = True

        with patch("rock.utils.http.HttpUtils.post", new_callable=AsyncMock) as mock_post, patch.object(
            sandbox, "get_status", return_value=mock_status
        ):
            mock_post.return_value = mock_response
            await sandbox.start()

            call_args = mock_post.call_args
            posted_data = call_args[0][2]
            assert posted_data["operator_type"] == "ray"

    @pytest.mark.asyncio
    async def test_start_payload_contains_all_expected_fields(self):
        """start() payload should contain operator_type alongside all other fields."""
        config = SandboxConfig(
            image="ubuntu:22.04",
            memory="16g",
            cpus=4,
            operator_type="k8s",
            startup_timeout=5,
        )
        sandbox = Sandbox(config)

        mock_response = {
            "status": "Success",
            "result": {
                "sandbox_id": "test-sandbox-004",
                "host_name": "test-host",
                "host_ip": "10.0.0.1",
            },
        }

        mock_status = AsyncMock()
        mock_status.is_alive = True

        with patch("rock.utils.http.HttpUtils.post", new_callable=AsyncMock) as mock_post, patch.object(
            sandbox, "get_status", return_value=mock_status
        ):
            mock_post.return_value = mock_response
            await sandbox.start()

            call_args = mock_post.call_args
            posted_data = call_args[0][2]

            # Verify all expected fields are present
            assert posted_data["image"] == "ubuntu:22.04"
            assert posted_data["memory"] == "16g"
            assert posted_data["cpus"] == 4
            assert posted_data["operator_type"] == "k8s"
            assert "use_kata_runtime" in posted_data
            assert "registry_username" in posted_data
            assert "registry_password" in posted_data


# ===========================================================================
# 4. SandboxGroup propagates operator_type
# ===========================================================================


class TestSandboxGroupOperatorType:
    """Test that SandboxGroup propagates operator_type to child sandboxes."""

    def test_group_propagates_operator_type_to_children(self):
        """All sandboxes in a group should inherit operator_type from config."""
        config = SandboxGroupConfig(
            size=3,
            operator_type="k8s",
        )
        group = SandboxGroup(config)

        assert len(group.sandbox_list) == 3
        for sandbox in group.sandbox_list:
            assert sandbox.config.operator_type == "k8s"

    def test_group_propagates_default_operator_type(self):
        """All sandboxes in a group should have default operator_type='ray' when not set."""
        config = SandboxGroupConfig(size=2)
        group = SandboxGroup(config)

        for sandbox in group.sandbox_list:
            assert sandbox.config.operator_type == "ray"


# ===========================================================================
# 5. SandboxGroupConfig inherits operator_type
# ===========================================================================


class TestSandboxGroupConfigOperatorType:
    """Test SandboxGroupConfig inherits operator_type from SandboxConfig."""

    def test_group_config_has_operator_type(self):
        """SandboxGroupConfig should support operator_type field."""
        config = SandboxGroupConfig(operator_type="ray", size=2)
        assert config.operator_type == "ray"

    def test_group_config_operator_type_default_ray(self):
        """SandboxGroupConfig.operator_type should default to 'ray'."""
        config = SandboxGroupConfig(size=2)
        assert config.operator_type == "ray"
