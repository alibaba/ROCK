"""Optional deployment-configured image resolution for E2B sandbox starts."""

from inspect import signature
from typing import Protocol

from rock.config import ImageResolverConfig
from rock.utils.http_pool import HttpPoolManager
from rock.utils.importer import safe_import_class
from rock.utils.providers.redis_provider import RedisProvider


class ImageResolver(Protocol):
    async def resolve(self, image: str) -> str:
        """Return a replacement image, or the original when resolution is unavailable."""
        ...


def create_image_resolver(
    config: ImageResolverConfig,
    http_pool_manager: HttpPoolManager,
    redis_provider: RedisProvider | None = None,
) -> ImageResolver | None:
    """Create one plugin, borrowing a client owned by the admin HTTP pool manager."""
    if not config.resolver_class:
        return None
    resolver_class = safe_import_class(config.resolver_class)
    if not isinstance(resolver_class, type):
        raise ValueError(f"Invalid image resolver class: {config.resolver_class}")
    options = dict(config.options)
    if "redis_provider" in signature(resolver_class).parameters:
        options["redis_provider"] = redis_provider
    return resolver_class(client=http_pool_manager.get("probe"), **options)
