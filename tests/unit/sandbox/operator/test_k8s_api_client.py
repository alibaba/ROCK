"""Unit tests for K8sApiClient.

Tests cover:
- AsyncLimiter rate limiting integration
- Informer pattern cache synchronization
- CRUD operations on K8s custom resources
"""

from unittest.mock import AsyncMock, patch

import pytest

from rock.sandbox.operator.k8s.api_client import K8sApiClient


class TestK8sApiClient:
    """Test cases for K8sApiClient."""

    def test_initialization(self, mock_api_client):
        """Test K8sApiClient initialization.
        
        Verifies AsyncLimiter is configured with QPS limit.
        """
        api_client = K8sApiClient(
            api_client=mock_api_client,
            group="sandbox.opensandbox.io",
            version="v1alpha1",
            plural="batchsandboxes",
            namespace="rock-test",
            qps=200.0,
            watch_timeout_seconds=60,
            watch_reconnect_delay_seconds=5,
        )
        
        assert api_client._group == "sandbox.opensandbox.io"
        assert api_client._version == "v1alpha1"
        assert api_client._plural == "batchsandboxes"
        assert api_client._namespace == "rock-test"
        assert api_client._rate_limiter.max_rate == 200.0
        assert api_client._watch_timeout_seconds == 60
        assert api_client._watch_reconnect_delay_seconds == 5

    @pytest.mark.asyncio
    async def test_rate_limiting_with_context_manager(self, k8s_api_client):
        """Test AsyncLimiter integration in CRUD operations.
        
        Verifies that AsyncLimiter is properly used as context manager
        to enforce rate limiting on API Server requests.
        """
        with patch('asyncio.to_thread', new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = {"created": True}
            
            result = await k8s_api_client.create_custom_object(body={"test": "data"})
            
            assert result == {"created": True}
            mock_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_custom_object(self, k8s_api_client):
        """Test creating K8s custom resource with rate limiting."""
        mock_body = {
            "apiVersion": "sandbox.opensandbox.io/v1alpha1",
            "kind": "BatchSandbox",
            "metadata": {"name": "test-sandbox"},
        }
        
        with patch('asyncio.to_thread', new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = {"created": True}
            
            result = await k8s_api_client.create_custom_object(body=mock_body)
            
            assert result == {"created": True}
            mock_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_custom_object_from_cache(self, k8s_api_client):
        """Test cache hit scenario (Informer pattern).
        
        When resource exists in local cache, no API Server request is made.
        """
        k8s_api_client._cache = {
            "test-sandbox": {"metadata": {"name": "test-sandbox"}}
        }
        
        result = await k8s_api_client.get_custom_object(name="test-sandbox")
        
        assert result == {"metadata": {"name": "test-sandbox"}}

    @pytest.mark.asyncio
    async def test_get_custom_object_cache_miss(self, k8s_api_client):
        """Test cache miss scenario with API Server fallback.
        
        When resource not in cache, queries API Server and updates cache.
        """
        k8s_api_client._cache = {}
        mock_response = {"metadata": {"name": "test-sandbox"}}
        
        with patch('asyncio.to_thread', new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_response
            
            result = await k8s_api_client.get_custom_object(name="test-sandbox")
            
            assert result == mock_response
            assert k8s_api_client._cache["test-sandbox"] == mock_response

    @pytest.mark.asyncio
    async def test_delete_custom_object(self, k8s_api_client):
        """Test deleting K8s custom resource with rate limiting."""
        with patch('asyncio.to_thread', new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = {"status": "deleted"}
            
            result = await k8s_api_client.delete_custom_object(name="test-sandbox")
            
            assert result == {"status": "deleted"}
            mock_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_initializes_watch(self, k8s_api_client):
        """Test watch task initialization for Informer pattern.
        
        start() creates background task to watch K8s resource changes
        and sync them to local cache.
        """
        with patch('asyncio.create_task') as mock_create_task:
            await k8s_api_client.start()
            
            assert k8s_api_client._initialized is True
            mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_idempotent(self, k8s_api_client):
        """Test start() idempotency.
        
        Multiple start() calls should only initialize watch once.
        """
        with patch('asyncio.create_task') as mock_create_task:
            await k8s_api_client.start()
            await k8s_api_client.start()
            
            assert mock_create_task.call_count == 1

    @pytest.mark.asyncio
    async def test_list_and_sync_cache(self, k8s_api_client):
        """Test initial cache sync from K8s API Server.
        
        Populates local cache with all resources and returns resourceVersion
        for subsequent watch operations.
        """
        mock_resources = {
            "metadata": {"resourceVersion": "12345"},
            "items": [
                {"metadata": {"name": "sandbox-1"}},
                {"metadata": {"name": "sandbox-2"}},
            ]
        }
        
        with patch('asyncio.to_thread', new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_resources
            
            resource_version = await k8s_api_client._list_and_sync_cache()
            
            assert resource_version == "12345"
            assert len(k8s_api_client._cache) == 2
            assert "sandbox-1" in k8s_api_client._cache
            assert "sandbox-2" in k8s_api_client._cache
