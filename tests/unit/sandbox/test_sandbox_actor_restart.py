"""Unit tests for SandboxActor.restart() — exit actor on failure."""

from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from rock.deployments.docker import DockerDeployment


@pytest.fixture(autouse=True)
def mock_exit_actor():
    with patch("ray.actor.exit_actor") as m:
        yield m


@pytest.fixture
def actor():
    from rock.sandbox.sandbox_actor import SandboxActor

    ActorClass = SandboxActor.__ray_actor_class__

    config = MagicMock()
    config.container_name = "sbx-restart-test"
    deployment = create_autospec(DockerDeployment, instance=True)
    deployment.restart = AsyncMock()

    instance = ActorClass.__new__(ActorClass)
    instance._config = config
    instance._deployment = deployment
    instance._clean_container_background = MagicMock()
    instance._setup_monitor = AsyncMock()
    return instance


class TestSandboxActorRestart:
    async def test_restart_success_no_exit(self, actor, mock_exit_actor):
        """On successful restart, actor stays alive."""
        await actor.restart()

        actor._deployment.restart.assert_called_once()
        actor._clean_container_background.assert_called_once()
        actor._setup_monitor.assert_called_once()
        mock_exit_actor.assert_not_called()

    async def test_restart_failure_exits_actor(self, actor, mock_exit_actor):
        """On restart failure, actor exits so it doesn't linger."""
        actor._deployment.restart = AsyncMock(side_effect=RuntimeError("docker start failed"))

        await actor.restart()

        mock_exit_actor.assert_called_once()
        actor._setup_monitor.assert_not_called()

    async def test_restart_monitor_failure_exits_actor(self, actor, mock_exit_actor):
        """If _setup_monitor fails after restart, actor still exits."""
        actor._setup_monitor = AsyncMock(side_effect=OSError("monitor setup failed"))

        await actor.restart()

        mock_exit_actor.assert_called_once()
