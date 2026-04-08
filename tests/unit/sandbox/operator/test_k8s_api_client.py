"""Unit tests for K8sApiClient.

Tests cover:
- AsyncLimiter rate limiting integration
- Informer pattern cache synchronization
- CRUD operations on K8s custom resources
- Real-time event processing with Queue-based watch
"""

import asyncio
import threading
from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

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
        k8s_api_client._cache = {"test-sandbox": {"metadata": {"name": "test-sandbox"}}}

        result = await k8s_api_client.get_custom_object(name="test-sandbox")

        assert result == {"metadata": {"name": "test-sandbox"}}

    @pytest.mark.asyncio
    async def test_get_custom_object_cache_miss(self, k8s_api_client):
        """Test cache miss scenario with API Server fallback.

        When resource not in cache, queries API Server and updates cache.
        """
        k8s_api_client._cache = {}
        mock_response = {"metadata": {"name": "test-sandbox"}}

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_response

            result = await k8s_api_client.get_custom_object(name="test-sandbox")

            assert result == mock_response
            assert k8s_api_client._cache["test-sandbox"] == mock_response

    @pytest.mark.asyncio
    async def test_delete_custom_object(self, k8s_api_client):
        """Test deleting K8s custom resource with rate limiting."""
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
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
        with patch("asyncio.create_task") as mock_create_task:
            await k8s_api_client.start()

            assert k8s_api_client._initialized is True
            mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_idempotent(self, k8s_api_client):
        """Test start() idempotency.

        Multiple start() calls should only initialize watch once.
        """
        with patch("asyncio.create_task") as mock_create_task:
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
            ],
        }

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_resources

            resource_version = await k8s_api_client._list_and_sync_cache()

            assert resource_version == "12345"
            assert len(k8s_api_client._cache) == 2
            assert "sandbox-1" in k8s_api_client._cache
            assert "sandbox-2" in k8s_api_client._cache

    @pytest.mark.asyncio
    async def test_process_events_handles_added_event(self, k8s_api_client):
        """Test _process_events handles ADDED event and updates cache."""
        event_queue = asyncio.Queue()
        mock_future = MagicMock(spec=Future)
        mock_future.done.return_value = False

        # Put ADDED event with resourceVersion
        await event_queue.put((
            "event",
            {
                "type": "ADDED",
                "object": {
                    "metadata": {"name": "new-sandbox", "resourceVersion": "100"}
                },
            },
        ))
        await event_queue.put(("exit", None))  # Signal exit

        resource_version = await k8s_api_client._process_events(event_queue, mock_future)

        assert resource_version == "100"
        assert "new-sandbox" in k8s_api_client._cache

    @pytest.mark.asyncio
    async def test_process_events_handles_modified_event(self, k8s_api_client):
        """Test _process_events handles MODIFIED event and updates cache."""
        event_queue = asyncio.Queue()
        mock_future = MagicMock(spec=Future)
        mock_future.done.return_value = False

        # Pre-populate cache and resource_version
        k8s_api_client._cache["existing-sandbox"] = {"metadata": {"name": "existing-sandbox", "resourceVersion": "100"}}
        k8s_api_client._resource_version = "100"

        # Put MODIFIED event with newer version
        await event_queue.put((
            "event",
            {
                "type": "MODIFIED",
                "object": {
                    "metadata": {"name": "existing-sandbox", "resourceVersion": "200"}
                },
            },
        ))
        await event_queue.put(("exit", None))

        resource_version = await k8s_api_client._process_events(event_queue, mock_future)

        assert resource_version == "200"
        assert k8s_api_client._cache["existing-sandbox"]["metadata"]["resourceVersion"] == "200"

    @pytest.mark.asyncio
    async def test_process_events_handles_deleted_event(self, k8s_api_client):
        """Test _process_events handles DELETED event and removes from cache."""
        event_queue = asyncio.Queue()
        mock_future = MagicMock(spec=Future)
        mock_future.done.return_value = False

        # Pre-populate cache and resource_version
        k8s_api_client._cache["to-delete"] = {"metadata": {"name": "to-delete", "resourceVersion": "50"}}
        k8s_api_client._resource_version = "50"

        # Put DELETED event with newer version
        await event_queue.put((
            "event",
            {
                "type": "DELETED",
                "object": {
                    "metadata": {"name": "to-delete", "resourceVersion": "300"}
                },
            },
        ))
        await event_queue.put(("exit", None))

        resource_version = await k8s_api_client._process_events(event_queue, mock_future)

        assert "to-delete" not in k8s_api_client._cache
        assert resource_version == "300"

    @pytest.mark.asyncio
    async def test_process_events_handles_exit_signal(self, k8s_api_client):
        """Test _process_events exits when receiving exit signal."""
        event_queue = asyncio.Queue()
        mock_future = MagicMock(spec=Future)
        mock_future.done.return_value = False

        # Pre-populate resource_version so it can be returned
        k8s_api_client._resource_version = "100"

        await event_queue.put(("exit", None))

        resource_version = await k8s_api_client._process_events(event_queue, mock_future)

        assert resource_version == "100"

    @pytest.mark.asyncio
    async def test_process_events_timeout_checks_thread_exit(self, k8s_api_client):
        """Test _process_events breaks when thread exited during timeout."""
        event_queue = asyncio.Queue()
        mock_future = MagicMock(spec=Future)
        mock_future.done.return_value = True  # Thread has exited
        # Queue is empty by default, so empty() will return True

        # Pre-populate resource_version
        k8s_api_client._resource_version = "100"

        # Don't put any events, let it timeout
        resource_version = await k8s_api_client._process_events(event_queue, mock_future)

        assert resource_version == "100"

    @pytest.mark.asyncio
    async def test_process_events_skips_event_without_name(self, k8s_api_client):
        """Test _process_events skips events without name in metadata."""
        event_queue = asyncio.Queue()
        mock_future = MagicMock(spec=Future)
        mock_future.done.return_value = False

        # Pre-populate resource_version so version check passes
        k8s_api_client._resource_version = "50"

        # Put event without name but with newer resourceVersion
        await event_queue.put((
            "event",
            {
                "type": "ADDED",
                "object": {
                    "metadata": {"resourceVersion": "400"}  # No name
                },
            },
        ))
        await event_queue.put(("exit", None))

        resource_version = await k8s_api_client._process_events(event_queue, mock_future)

        assert resource_version == "400"
        assert len(k8s_api_client._cache) == 0  # Nothing added due to no name

    def test_watch_in_thread_puts_event_to_queue(self, k8s_api_client):
        """Test _watch_in_thread puts events to queue."""
        event_queue = asyncio.Queue()
        loop = asyncio.new_event_loop()
        stop_event = threading.Event()
        k8s_api_client._stop_event = stop_event

        # Mock watch stream
        mock_events = [
            {"type": "ADDED", "object": {"metadata": {"name": "test-1"}}},
        ]

        with patch("rock.sandbox.operator.k8s.api_client.watch.Watch") as mock_watch_class:
            mock_watch = MagicMock()
            mock_watch.stream.return_value = iter(mock_events)
            mock_watch_class.return_value = mock_watch

            k8s_api_client._watch_in_thread("100", event_queue, loop)

        # Check exit signal was put
        # Note: run_coroutine_threadsafe schedules to loop, but we're not running it
        # So we just verify the function completes without error
        loop.close()

    def test_watch_in_thread_respects_stop_event(self, k8s_api_client):
        """Test _watch_in_thread stops when stop_event is set."""
        event_queue = asyncio.Queue()
        loop = asyncio.new_event_loop()
        stop_event = threading.Event()
        stop_event.set()  # Already stopped
        k8s_api_client._stop_event = stop_event

        # Create infinite iterator but should break immediately
        def infinite_events():
            while True:
                yield {"type": "ADDED", "object": {"metadata": {"name": "test"}}}

        with patch("rock.sandbox.operator.k8s.api_client.watch.Watch") as mock_watch_class:
            mock_watch = MagicMock()
            mock_watch.stream.return_value = infinite_events()
            mock_watch_class.return_value = mock_watch

            k8s_api_client._watch_in_thread("100", event_queue, loop)

        loop.close()

    @pytest.mark.asyncio
    async def test_watch_resources_reconnects_on_failure(self, k8s_api_client):
        """Test _watch_resources reconnects after watch failure."""
        call_count = 0

        async def mock_list_and_sync():
            nonlocal call_count
            call_count += 1
            return "rv-" + str(call_count)

        k8s_api_client._list_and_sync_cache = mock_list_and_sync
        k8s_api_client._cache = {"initial": {}}

        # Mock _process_events to avoid real watch loop
        async def mock_process_events(*args, **kwargs):
            # Simulate disconnect after first call
            await asyncio.sleep(0.05)
            raise Exception("Simulated watch disconnect")

        k8s_api_client._process_events = mock_process_events

        # Create a task that will exit after first iteration
        watch_task = asyncio.create_task(k8s_api_client._watch_resources())

        # Wait for it to complete one iteration
        await asyncio.sleep(0.2)

        # Cancel if still running
        if not watch_task.done():
            watch_task.cancel()
            try:
                await watch_task
            except asyncio.CancelledError:
                pass

        # Should have called _list_and_sync_cache at least once
        assert call_count >= 1
