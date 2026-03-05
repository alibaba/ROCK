from unittest.mock import AsyncMock, patch

import pytest

from rock.sdk.sandbox.client import Sandbox, SandboxGroup
from rock.sdk.sandbox.config import SandboxConfig, SandboxGroupConfig


def test_sandbox_stores_operator_type_from_config():
    """Sandbox should store the config with operator_type."""
    config = SandboxConfig(operator_type="k8s")
    sandbox = Sandbox(config)
    assert sandbox.config.operator_type == "k8s"


def test_sandbox_stores_default_operator_type():
    """Sandbox should store default operator_type='ray' when not specified."""
    config = SandboxConfig()
    sandbox = Sandbox(config)
    assert sandbox.config.operator_type == "ray"


@pytest.mark.asyncio
async def test_start_sends_operator_type_in_payload():
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

    mock_status = AsyncMock()
    mock_status.is_alive = True

    with patch("rock.utils.http.HttpUtils.post", new_callable=AsyncMock) as mock_post, patch.object(
        sandbox, "get_status", return_value=mock_status
    ):
        mock_post.return_value = mock_response
        await sandbox.start()

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        posted_data = call_args[0][2]
        assert "operator_type" in posted_data
        assert posted_data["operator_type"] == "k8s"


@pytest.mark.asyncio
async def test_start_sends_default_operator_type_when_not_set():
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
async def test_start_sends_ray_operator_type():
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
async def test_start_payload_contains_all_expected_fields():
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

        assert posted_data["image"] == "ubuntu:22.04"
        assert posted_data["memory"] == "16g"
        assert posted_data["cpus"] == 4
        assert posted_data["operator_type"] == "k8s"
        assert "use_kata_runtime" in posted_data
        assert "registry_username" in posted_data
        assert "registry_password" in posted_data


def test_group_propagates_operator_type_to_children():
    """All sandboxes in a group should inherit operator_type from config."""
    config = SandboxGroupConfig(size=3, operator_type="k8s")
    group = SandboxGroup(config)

    assert len(group.sandbox_list) == 3
    for sandbox in group.sandbox_list:
        assert sandbox.config.operator_type == "k8s"


def test_group_propagates_default_operator_type():
    """All sandboxes in a group should have default operator_type='ray' when not set."""
    config = SandboxGroupConfig(size=2)
    group = SandboxGroup(config)

    for sandbox in group.sandbox_list:
        assert sandbox.config.operator_type == "ray"
