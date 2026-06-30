"""Unit tests for DeploymentManager.init_config FC branch.

Verifies review findings:
- Phase 4: FCOperatorConfig is preserved (not converted to RayDeploymentConfig)
- S22: init_config mutates input in-place instead of returning a copy
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from rock.config import FCConfig
from rock.deployments.config import RayDeploymentConfig
from rock.deployments.manager import DeploymentManager
from rock.sandbox.operator.fc.config import FCOperatorConfig


@pytest.fixture
def fc_config() -> FCConfig:
    return FCConfig(
        region="cn-hangzhou",
        account_id="1234567890",
        access_key_id="AKIDTEST",
        access_key_secret="SKTEST",
    )


@pytest.fixture
def deployment_manager(fc_config):
    rock_config = MagicMock()
    rock_config.fc = fc_config
    rock_config.update = AsyncMock()
    rock_config.runtime = MagicMock()
    rock_config.sandbox_config = MagicMock()
    return DeploymentManager(rock_config)


class TestInitConfigFC:
    async def test_preserves_fc_config_type(self, deployment_manager):
        config = FCOperatorConfig(session_id="fc-existing", image="img")
        result = await deployment_manager.init_config(config)
        assert isinstance(result, FCOperatorConfig)
        assert not isinstance(result, RayDeploymentConfig)

    async def test_generates_session_id_when_missing(self, deployment_manager):
        config = FCOperatorConfig(image="img")
        result = await deployment_manager.init_config(config)
        assert result.session_id is not None
        assert result.session_id.startswith("fc-")

    async def test_keeps_existing_session_id(self, deployment_manager):
        config = FCOperatorConfig(session_id="fc-keep", image="img")
        result = await deployment_manager.init_config(config)
        assert result.session_id == "fc-keep"

    @pytest.mark.xfail(reason="S22: init_config mutates input in-place instead of returning a copy")
    async def test_does_not_mutate_input(self, deployment_manager):
        config = FCOperatorConfig(image="img")
        original_id = config.session_id
        await deployment_manager.init_config(config)
        assert config.session_id == original_id, "input config must not be mutated"
