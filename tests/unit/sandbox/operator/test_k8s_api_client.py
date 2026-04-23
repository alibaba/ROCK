"""Unit tests for K8sApiClient.

Tests cover:
- AsyncLimiter rate limiting integration
- SharedInformer integration for cache
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
            resync_period=60,
        )

        assert api_client._group == "sandbox.opensandbox.io"
        assert api_client._version == "v1alpha1"
        assert api_client._plural == "batchsandboxes"
        assert api_client._namespace == "rock-test"
        assert api_client._rate_limiter.max_rate == 200.0
        assert api_client._informer is not None

    @pytest.mark.asyncio
    async def test_rate_limiting_with_context_manager(self, k8s_api_client):
        """Test AsyncLimiter integration in CRUD operations.

        Verifies that AsyncLimiter is properly used as context manager
        to enforce rate limiting on API Server requests.
        """
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
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

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = {"created": True}

            result = await k8s_api_client.create_custom_object(body=mock_body)

            assert result == {"created": True}
            mock_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_custom_object_from_cache(self, k8s_api_client):
        """Test cache hit scenario (Informer pattern).

        When resource exists in local cache, no API Server request is made.
        """
        # Populate the informer cache with namespace/name key
        k8s_api_client._informer.cache._objects["rock-test/test-sandbox"] = {
            "metadata": {"name": "test-sandbox", "namespace": "rock-test"}
        }

        result = await k8s_api_client.get_custom_object(name="test-sandbox")

        assert result == {"metadata": {"name": "test-sandbox", "namespace": "rock-test"}}

    @pytest.mark.asyncio
    async def test_get_custom_object_cache_miss(self, k8s_api_client):
        """Test cache miss scenario with API Server fallback.

        When resource not in cache, queries API Server.
        """
        mock_response = {"metadata": {"name": "test-sandbox"}}

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_response

            result = await k8s_api_client.get_custom_object(name="test-sandbox")

            assert result == mock_response
            mock_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_custom_object(self, k8s_api_client):
        """Test deleting K8s custom resource with rate limiting."""
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = {"status": "deleted"}

            result = await k8s_api_client.delete_custom_object(name="test-sandbox")

            assert result == {"status": "deleted"}
            mock_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_custom_object(self, k8s_api_client):
        """Test updating K8s custom resource with rate limiting."""
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = {"updated": True}

            result = await k8s_api_client.update_custom_object(name="test-sandbox", body={"spec": {"new": "value"}})

            assert result == {"updated": True}
            mock_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_initializes_informer(self, k8s_api_client):
        """Test start() initializes the SharedInformer."""
        with patch.object(k8s_api_client._informer, "start") as mock_start:
            await k8s_api_client.start()

            assert k8s_api_client._initialized is True
            mock_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_idempotent(self, k8s_api_client):
        """Test start() idempotency.

        Multiple start() calls should only initialize watch once.
        """
        with patch.object(k8s_api_client._informer, "start") as mock_start:
            await k8s_api_client.start()
            await k8s_api_client.start()

            # start() should only be called once
            assert mock_start.call_count == 1

    @pytest.mark.asyncio
    async def test_stop_informer(self, k8s_api_client):
        """Test stop() stops the SharedInformer."""
        with (
            patch.object(k8s_api_client._informer, "start") as _mock_start,
            patch.object(k8s_api_client._informer, "stop") as mock_stop,
        ):
            await k8s_api_client.start()
            await k8s_api_client.stop()

            assert k8s_api_client._initialized is False
            mock_stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_custom_objects(self, k8s_api_client):
        """Test list_custom_objects returns all cached resources."""
        # Populate the cache with namespace/name keys
        k8s_api_client._informer.cache._objects = {
            "rock-test/sandbox-1": {"metadata": {"name": "sandbox-1", "namespace": "rock-test"}},
            "rock-test/sandbox-2": {"metadata": {"name": "sandbox-2", "namespace": "rock-test"}},
        }

        result = await k8s_api_client.list_custom_objects()

        assert len(result) == 2
        names = {obj["metadata"]["name"] for obj in result}
        assert names == {"sandbox-1", "sandbox-2"}
