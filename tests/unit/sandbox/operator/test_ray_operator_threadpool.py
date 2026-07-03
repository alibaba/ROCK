"""Unit tests verifying Ray operator dispatches blocking Ray calls to the thread pool.

The `.remote()` family (create_actor, actor method calls) are blocking GCS RPCs.
These must run inside the thread pool executor so the asyncio event loop stays
responsive under concurrency. These tests assert that pattern is preserved.
"""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock import InternalServerRockError
from rock.config import RuntimeConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.ray import RayOperator


@pytest.fixture
def operator():
    ray_service = MagicMock()
    ray_service.get_ray_rwlock.return_value.read_lock.return_value = AsyncMock()
    ray_service.async_ray_get = AsyncMock(return_value={})
    executor = MagicMock()
    ray_service._executor = executor
    op = RayOperator(ray_service=ray_service, runtime_config=RuntimeConfig())
    return op


def _make_config(container_name="sbx-1"):
    return DockerDeploymentConfig(container_name=container_name, cpus=1, memory="1g")


class TestCreateActorIsSync:
    """create_actor must be a plain sync function — no await inside."""

    def test_not_a_coroutine_function(self, operator):
        assert not inspect.iscoroutinefunction(operator.create_actor)

    @patch("rock.sandbox.operator.ray.SandboxActor")
    def test_returns_actor_handle(self, mock_actor_cls, operator):
        mock_actor = MagicMock()
        mock_actor_cls.options.return_value.remote.return_value = mock_actor
        config = _make_config()

        result = operator.create_actor(config)

        assert result is mock_actor


class TestSubmitDispatchesToExecutor:
    """submit must run all .remote() calls inside the thread pool executor."""

    @patch("rock.sandbox.operator.ray.SandboxActor")
    @pytest.mark.asyncio
    async def test_submit_runs_in_executor(self, mock_actor_cls, operator):
        mock_actor = MagicMock()
        mock_actor_cls.options.return_value.remote.return_value = mock_actor
        mock_actor.sandbox_info.remote.return_value = MagicMock()

        # Make run_in_executor actually invoke the callable synchronously
        loop = asyncio.get_running_loop()

        async def fake_run_in_executor(executor, fn, *args):
            return fn(*args) if args else fn()

        with patch.object(loop, "run_in_executor", side_effect=fake_run_in_executor) as mock_run:
            with patch("rock.sandbox.operator.ray.ray") as mock_ray:
                mock_ray.get.return_value = {"host_ip": "10.0.0.1"}
                config = _make_config()
                result = await operator.submit(config, user_info={"user_id": "u1"})

        assert mock_run.called
        called_executor = mock_run.call_args[0][0]
        assert called_executor is operator._ray_service._executor
        assert result["user_id"] == "u1"
        mock_actor.start.remote.assert_called_once()

    @patch("rock.sandbox.operator.ray.SandboxActor")
    @pytest.mark.asyncio
    async def test_submit_blocks_event_loop_only_briefly(self, mock_actor_cls, operator):
        """The blocking GCS RPC work must happen inside the executor, not on the loop."""
        mock_actor = MagicMock()
        mock_actor_cls.options.return_value.remote.return_value = mock_actor
        mock_actor.sandbox_info.remote.return_value = MagicMock()

        call_threads = {}

        async def fake_run_in_executor(executor, fn, *args):
            # Record which thread the callable runs on
            import threading

            call_threads["executor_thread"] = threading.current_thread()
            return fn(*args) if args else fn()

        loop = asyncio.get_running_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_run_in_executor):
            with patch("rock.sandbox.operator.ray.ray") as mock_ray:
                mock_ray.get.return_value = {}
                await operator.submit(_make_config())

        # The callable must NOT have run on the asyncio thread
        assert call_threads["executor_thread"] is not asyncio.get_event_loop_policy().get_event_loop()

    @patch("rock.sandbox.operator.ray.SandboxActor")
    @pytest.mark.asyncio
    async def test_submit_timeout_raises_internal_server_error(self, mock_actor_cls, operator):
        """TimeoutError must be wrapped into InternalServerRockError, not leaked raw."""
        mock_actor = MagicMock()
        mock_actor_cls.options.return_value.remote.return_value = mock_actor

        async def slow_run_in_executor(executor, fn, *args):
            # Simulate a hanging executor task — never returns, just wait forever
            await asyncio.sleep(10)

        loop = asyncio.get_running_loop()
        with patch.object(loop, "run_in_executor", side_effect=slow_run_in_executor):
            # Patch asyncio.wait_for to use a tiny timeout so the test doesn't actually wait
            with patch("rock.sandbox.operator.ray.asyncio.wait_for", side_effect=asyncio.TimeoutError):
                with pytest.raises(InternalServerRockError, match="submit timed out"):
                    await operator.submit(_make_config())


class TestDeleteDispatchesCreateActorToExecutor:
    """delete must dispatch create_actor to the thread pool, not call it inline."""

    @patch("rock.sandbox.operator.ray.SandboxActor")
    @pytest.mark.asyncio
    async def test_delete_create_actor_in_executor(self, mock_actor_cls, operator):
        mock_actor = MagicMock()
        mock_actor_cls.options.return_value.remote.return_value = mock_actor
        operator._ray_service.async_ray_get_actor = AsyncMock(return_value=MagicMock())

        executor_calls = []

        async def fake_run_in_executor(executor, fn, *args):
            executor_calls.append(executor)
            return fn() if not args else fn(*args)

        loop = asyncio.get_running_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_run_in_executor):
            with patch("rock.sandbox.operator.ray.ray") as mock_ray:
                mock_ray.kill = MagicMock()
                await operator.delete(_make_config(), host_ip="10.0.0.1")

        assert len(executor_calls) >= 1
        assert executor_calls[0] is operator._ray_service._executor


class TestRestartDispatchesCreateActorToExecutor:
    """restart must dispatch create_actor to the thread pool, not call it inline."""

    @patch("rock.sandbox.operator.ray.SandboxActor")
    @pytest.mark.asyncio
    async def test_restart_create_actor_in_executor(self, mock_actor_cls, operator):
        mock_actor = MagicMock()
        mock_actor_cls.options.return_value.remote.return_value = mock_actor
        operator._ray_service.async_ray_get = AsyncMock(return_value={})

        executor_calls = []

        async def fake_run_in_executor(executor, fn, *args):
            executor_calls.append(executor)
            return fn() if not args else fn(*args)

        loop = asyncio.get_running_loop()
        with patch.object(loop, "run_in_executor", side_effect=fake_run_in_executor):
            await operator.restart(_make_config(), host_ip="10.0.0.1")

        assert len(executor_calls) >= 1
        assert executor_calls[0] is operator._ray_service._executor
