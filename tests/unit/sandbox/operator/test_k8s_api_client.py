"""Unit tests for K8sApiClient."""

from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import pytest

try:
    from rock.sandbox.operator.k8s.api_client import K8sApiClient
    from kubernetes import client
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False


pytestmark = pytest.mark.skipif(not K8S_AVAILABLE, reason="kubernetes library not available")


@pytest.fixture
def mock_api_client():
    """Create mock K8S ApiClient."""
    return MagicMock(spec=client.ApiClient)


@pytest.fixture
def k8s_api_client(mock_api_client):
    """Create K8sApiClient instance."""
    return K8sApiClient(
        api_client=mock_api_client,
        group="sandbox.opensandbox.io",
        version="v1alpha1",
        plural="batchsandboxes",
        namespace="rock-test",
        qps=5.0,
        burst=10,
        watch_timeout_seconds=60,
        watch_reconnect_delay_seconds=5,
    )


class TestK8sApiClient:
    """Test cases for K8sApiClient."""

    def test_initialization(self, mock_api_client):
        """Test K8sApiClient initialization."""
        api_client = K8sApiClient(
            api_client=mock_api_client,
            group="sandbox.opensandbox.io",
            version="v1alpha1",
            plural="batchsandboxes",
            namespace="rock-test",
            qps=200.0,
            burst=400,
            watch_timeout_seconds=60,
            watch_reconnect_delay_seconds=5,
        )
        
        assert api_client._group == "sandbox.opensandbox.io"
        assert api_client._version == "v1alpha1"
        assert api_client._plural == "batchsandboxes"
        assert api_client._namespace == "rock-test"
        assert api_client._qps == 200.0
        assert api_client._burst == 400
        assert api_client._watch_timeout_seconds == 60
        assert api_client._watch_reconnect_delay_seconds == 5

    @pytest.mark.asyncio
    async def test_rate_limiting(self, k8s_api_client):
        """Test rate limiting functionality."""
        # Mock time to control rate limiting
        start_time = 1000.0
        times = [start_time, start_time, start_time + 0.1, start_time + 0.1]
        
        with patch('asyncio.get_event_loop') as mock_loop:
            mock_loop.return_value.time.side_effect = times
            
            # First call should not sleep
            await k8s_api_client._rate_limit()
            
            # Second call within interval should sleep
            with patch('asyncio.sleep') as mock_sleep:
                await k8s_api_client._rate_limit()
                # Should sleep for remaining time (0.2s - 0.1s = 0.1s)
                mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_custom_object(self, k8s_api_client):
        """Test creating a custom object."""
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
        """Test getting object from cache (cache hit)."""
        # Populate cache
        k8s_api_client._cache = {
            "test-sandbox": {"metadata": {"name": "test-sandbox"}}
        }
        
        result = await k8s_api_client.get_custom_object(name="test-sandbox")
        
        assert result == {"metadata": {"name": "test-sandbox"}}

    @pytest.mark.asyncio
    async def test_get_custom_object_cache_miss(self, k8s_api_client):
        """Test getting object when cache miss (fallback to API)."""
        k8s_api_client._cache = {}
        
        mock_response = {"metadata": {"name": "test-sandbox"}}
        
        with patch('asyncio.to_thread', new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_response
            
            result = await k8s_api_client.get_custom_object(name="test-sandbox")
            
            assert result == mock_response
            # Verify cache was updated
            assert k8s_api_client._cache["test-sandbox"] == mock_response

    @pytest.mark.asyncio
    async def test_delete_custom_object(self, k8s_api_client):
        """Test deleting a custom object."""
        with patch('asyncio.to_thread', new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = {"status": "deleted"}
            
            result = await k8s_api_client.delete_custom_object(name="test-sandbox")
            
            assert result == {"status": "deleted"}
            mock_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_initializes_watch(self, k8s_api_client):
        """Test that start() initializes watch task."""
        with patch('asyncio.create_task') as mock_create_task:
            await k8s_api_client.start()
            
            assert k8s_api_client._initialized is True
            mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_idempotent(self, k8s_api_client):
        """Test that start() is idempotent."""
        with patch('asyncio.create_task') as mock_create_task:
            await k8s_api_client.start()
            await k8s_api_client.start()  # Call twice
            
            # Should only create task once
            assert mock_create_task.call_count == 1

    @pytest.mark.asyncio
    async def test_list_and_sync_cache(self, k8s_api_client):
        """Test cache synchronization from list operation."""
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
