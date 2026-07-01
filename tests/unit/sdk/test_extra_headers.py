import pytest

from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.utils import HttpUtils


@pytest.mark.asyncio
async def test_xrl_authorization_deprecated(monkeypatch):
    # Used to store captured headers
    captured_headers = {}

    async def mock_post(url: str, headers: dict, data: dict) -> dict:
        # Capture headers for validation
        captured_headers.update(headers)

        # Return mock successful response
        return {
            "status": "Success",
            "result": {"sandbox_id": "test_sandbox_id", "host_name": "test_host", "host_ip": "127.0.0.1"},
        }

    async def mock_get(url: str, headers: dict) -> dict:
        # Mock get_status response
        return {"status": "Success", "result": {"is_alive": True, "host_name": "test_host"}}

    # Replace HttpUtils methods
    monkeypatch.setattr(HttpUtils, "post", mock_post)
    monkeypatch.setattr(HttpUtils, "get", mock_get)

    with pytest.warns(DeprecationWarning, match="XRL-Authorization is deprecated, use extra_headers instead"):
        config = SandboxConfig(image="mock_image", xrl_authorization="test_token")
        sandbox = Sandbox(config)
        await sandbox.start()

    # Verify headers
    assert "XRL-Authorization" in captured_headers
    assert captured_headers["XRL-Authorization"] == "Bearer test_token"


@pytest.mark.asyncio
async def test_extra_headers(monkeypatch):
    # Used to store captured headers
    captured_headers = {}

    async def mock_post(url: str, headers: dict, data: dict) -> dict:
        # Capture headers for validation
        captured_headers.update(headers)

        # Return mock successful response
        return {
            "status": "Success",
            "result": {"sandbox_id": "test_sandbox_id", "host_name": "test_host", "host_ip": "127.0.0.1"},
        }

    async def mock_get(url: str, headers: dict) -> dict:
        # Mock get_status response
        return {"status": "Success", "result": {"is_alive": True, "host_name": "test_host"}}

    # Replace HttpUtils methods
    monkeypatch.setattr(HttpUtils, "post", mock_post)
    monkeypatch.setattr(HttpUtils, "get", mock_get)

    config = SandboxConfig(image="mock_image", extra_headers={"XRL-Authorization": "Bearer test_token"})
    sandbox = Sandbox(config)
    await sandbox.start()

    # Verify headers
    assert "XRL-Authorization" in captured_headers
    assert captured_headers["XRL-Authorization"] == "Bearer test_token"


@pytest.mark.asyncio
async def test_extra_headers_with_xrl_authorization(monkeypatch):
    # Test conflict situation between extra_headers and xrl_authorization, extra_headers should take precedence
    # Used to store captured headers
    captured_headers = {}

    async def mock_post(url: str, headers: dict, data: dict) -> dict:
        # Capture headers for validation
        captured_headers.update(headers)

        # Return mock successful response
        return {
            "status": "Success",
            "result": {"sandbox_id": "test_sandbox_id", "host_name": "test_host", "host_ip": "127.0.0.1"},
        }

    async def mock_get(url: str, headers: dict) -> dict:
        # Mock get_status response
        return {"status": "Success", "result": {"is_alive": True, "host_name": "test_host"}}

    # Replace HttpUtils methods
    monkeypatch.setattr(HttpUtils, "post", mock_post)
    monkeypatch.setattr(HttpUtils, "get", mock_get)

    config = SandboxConfig(
        image="mock_image",
        extra_headers={"XRL-Authorization": "Bearer test_token_from_extra_headers"},
        xrl_authorization="test_token_from_xrl_authorization",
    )
    sandbox = Sandbox(config)
    await sandbox.start()

    # Verify headers
    assert "XRL-Authorization" in captured_headers
    assert captured_headers["XRL-Authorization"] == "Bearer test_token_from_extra_headers"
