"""Verify configured image resolver plugins through E2B sandbox start."""

from unittest.mock import AsyncMock

import pytest
import yaml

from rock.admin.service.e2b_service import E2BService
from rock.admin.service.image_resolver import create_image_resolver
from rock.config import RockConfig
from rock.deployments.config import DockerDeploymentConfig


class ExampleImageResolver:
    """An external plugin with deployment-specific options."""

    def __init__(self, *, client, target_registry):
        self.client = client
        self.target_registry = target_registry

    async def resolve(self, image: str) -> str:
        return f"{self.target_registry}/{image.split('/', 1)[1]}"


@pytest.mark.parametrize("config_source", ["yaml", "nacos"])
async def test_start_uses_configured_image_resolver(tmp_path, config_source):
    rock_config = RockConfig()
    manager = AsyncMock()
    templates = AsyncMock()
    templates.get_ready_template.return_value = {
        "image": "source.example.com/team/image:latest",
        "cpu_count": 2,
        "memory_mb": 4096,
        "disk_size_mb": 10240,
    }

    async def start_image():
        service = E2BService(
            manager,
            templates,
            image_resolver=create_image_resolver(rock_config.e2b_image_resolver, rock_config.http_pool_manager),
        )
        await service.start(DockerDeploymentConfig(image="template-id"))
        return manager.start_from_template.call_args.args[0].image

    try:
        assert await start_image() == "source.example.com/team/image:latest"
        plugin_config = {
            "e2b_image_resolver": {
                "resolver_class": "tests.unit.admin.service.test_e2b_image_resolution_config.ExampleImageResolver",
                "options": {"target_registry": "local.example.com"},
            }
        }
        if config_source == "yaml":
            config_path = tmp_path / "rock.yml"
            config_path.write_text(yaml.safe_dump(plugin_config))
            rock_config = RockConfig.from_env(str(config_path))
        else:
            rock_config.nacos_provider = AsyncMock()
            rock_config.nacos_provider.get_config.return_value = plugin_config
            await rock_config.update()
        assert await start_image() == "local.example.com/team/image:latest"
    finally:
        await rock_config.http_pool_manager.aclose_all()
