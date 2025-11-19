import pytest
import ray

from rock.actions import (
    BashObservation,
    CommandResponse,
)
from rock.admin.proto.request import SandboxBashAction as BashAction
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxCreateBashSessionRequest as CreateBashSessionRequest
from rock.deployments.config import LocalDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.sandbox_actor import SandboxActor

logger = init_logger(__name__)


@pytest.mark.need_ray
@pytest.mark.asyncio
async def test_execute(ray_init_shutdown):
    sandbox_config = LocalDeploymentConfig()
    sandbox_actor = SandboxActor.remote(sandbox_config, sandbox_config.get_deployment())
    sandbox_actor.start.remote()
    command_response_ref = sandbox_actor.execute.remote(Command(command="ls"))
    command_response: CommandResponse = ray.get(command_response_ref)
    assert "rock" in command_response.stdout
    logger.info(f"observation: {command_response.stdout}")

    sandbox_actor.create_session.remote(CreateBashSessionRequest())
    observation: BashObservation = ray.get(
        sandbox_actor.run_in_session.remote(BashAction(command="ls", action_type="bash"))
    )
    logger.info(f"observation: {observation}")
    assert "rock" in observation.output

    ray.get(sandbox_actor.run_in_session.remote(BashAction(command="cd rock", action_type="bash")))
    observation: BashObservation = ray.get(
        sandbox_actor.run_in_session.remote(BashAction(command="ls", action_type="bash"))
    )
    logger.info(f"observation: {observation}")
    assert "sdk" in observation.output
    ray.kill(sandbox_actor)


@pytest.mark.asyncio
@pytest.mark.need_ray
async def test_execute_with_additional_pkgs(ray_init_shutdown):
    sandbox_config = LocalDeploymentConfig(role="test", env="dev")
    actor_name = "sandbox-test"
    sandbox_actor = SandboxActor.options(name=actor_name).remote(sandbox_config, sandbox_config.get_deployment())
    actor_obj = ray.get_actor(actor_name)
    assert actor_obj
    ray.kill(sandbox_actor)
