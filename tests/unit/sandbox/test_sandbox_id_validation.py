"""Test sandbox_id validation in SandboxProxyService."""

from unittest.mock import MagicMock

import pytest

from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sdk.common.exceptions import BadRequestRockError


def _make_proxy_service():
    mock_config = MagicMock()
    mock_config.oss = MagicMock()
    mock_config.oss.access_key_id = ""
    mock_config.oss.access_key_secret = ""
    mock_config.oss.role_arn = ""
    mock_config.proxy_service = MagicMock()
    mock_config.proxy_service.timeout = 30
    mock_config.proxy_service.max_connections = 100
    mock_config.proxy_service.max_keepalive_connections = 20
    mock_config.proxy_service.batch_get_status_max_count = 100
    mock_config.runtime = MagicMock()
    mock_config.runtime.metrics_endpoint = ""
    mock_config.runtime.user_defined_tags = {}
    mock_meta_store = MagicMock()
    return SandboxProxyService(mock_config, meta_store=mock_meta_store)


@pytest.mark.asyncio
async def test_get_service_status_none_raises():
    svc = _make_proxy_service()
    with pytest.raises(BadRequestRockError, match="sandbox_id is required"):
        await svc.get_service_status(None)


@pytest.mark.asyncio
async def test_get_service_status_empty_raises():
    svc = _make_proxy_service()
    with pytest.raises(BadRequestRockError, match="sandbox_id is required"):
        await svc.get_service_status("")


@pytest.mark.asyncio
async def test_get_service_status_whitespace_raises():
    svc = _make_proxy_service()
    with pytest.raises(BadRequestRockError, match="sandbox_id is required"):
        await svc.get_service_status("   ")


@pytest.mark.asyncio
async def test_update_expire_time_none_raises():
    svc = _make_proxy_service()
    with pytest.raises(BadRequestRockError, match="sandbox_id is required"):
        await svc._update_expire_time(None)


@pytest.mark.asyncio
async def test_update_expire_time_empty_raises():
    svc = _make_proxy_service()
    with pytest.raises(BadRequestRockError, match="sandbox_id is required"):
        await svc._update_expire_time("")
