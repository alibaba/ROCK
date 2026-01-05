import pytest

from rock.actions import Command
from rock.logger import init_logger
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.model_service.base import ModelService, ModelServiceConfig
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = init_logger(__name__)


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_start(sandbox_instance: Sandbox):
    model_service_config = ModelServiceConfig()
    sandbox_instance.model_service = ModelService(sandbox_instance, model_service_config)

    await sandbox_instance.model_service.install()

    assert sandbox_instance.model_service.is_installed
    assert not sandbox_instance.model_service.is_started

    await sandbox_instance.model_service.start()

    assert sandbox_instance.model_service.is_started

    result = await sandbox_instance.execute(Command(command=["ls", model_service_config.workdir]))

    assert result.exit_code == 0
