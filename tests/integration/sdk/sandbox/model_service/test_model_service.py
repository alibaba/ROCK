import pytest

from rock import env_vars
from rock.actions import Command
from rock.logger import init_logger
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.model_service.base import ModelService, ModelServiceConfig
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = init_logger(__name__)


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_model_service_install_and_start(sandbox_instance: Sandbox):
    """Test model service installation and startup flow."""

    # 1. Initialize model service
    python_install_cmd = env_vars.ROCK_AGENT_PYTHON_INSTALL_CMD
    model_service_config = ModelServiceConfig(python_install_cmd=python_install_cmd)
    sandbox_instance.model_service = ModelService(sandbox_instance, model_service_config)

    # 2. Install service
    await sandbox_instance.model_service.install()
    assert sandbox_instance.model_service.is_installed, "Model service should be installed"
    assert not sandbox_instance.model_service.is_started, "Model service should not be started yet"

    # 3. Start service
    await sandbox_instance.model_service.start()
    assert sandbox_instance.model_service.is_started, "Model service should be started"

    # 4. Verify installed files
    result = await sandbox_instance.execute(Command(command=["ls", model_service_config.workdir]))
    logger.info(f"Work directory contents: {result.stdout}")
    assert result.exit_code == 0
    assert "cpython31114.tar.gz" in result.stdout, "Tar archive file missing"
    assert "python" in result.stdout, "Python directory missing"

    # 5. Verify Python executables
    python_bin_path = f"{model_service_config.workdir}/python/bin"
    result = await sandbox_instance.execute(Command(command="ls", cwd=python_bin_path))
    logger.info(f"Python bin directory contents: {result.stdout}")
    assert result.exit_code == 0

    expected_executables = {"rock", "python", "python3", "pip"}
    for executable in expected_executables:
        assert executable in result.stdout, f"Missing executable: {executable}"

    result = await sandbox_instance.execute(Command(command=["bash", "-c", "curl http://localhost:8080/health"]))

    assert result.exit_code == 0
    assert "healthy" in result.stdout
