import logging
import pytest

from rock.sdk.common.exceptions import BadRequestRockError, RockException
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.client import Sandbox

logger = logging.getLogger(__name__)

@pytest.mark.asyncio
async def test_exception_cpu_limit(sandbox_config: SandboxConfig):
    sandbox_config.cpus = 20
    sandbox = Sandbox(sandbox_config)
    with pytest.raises(BadRequestRockError) as ex:
        await sandbox.start()
    logger.info(f"Exception: {str(ex.value)}")
    await sandbox.stop()