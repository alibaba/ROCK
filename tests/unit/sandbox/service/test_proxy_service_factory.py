import pytest

from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService


@pytest.mark.asyncio
async def test_create_proxy_service_uses_opensandbox_subclass(rock_config, sandbox_proxy_service):
    from rock.sandbox.service.factory import create_sandbox_proxy_service
    from rock.sandbox.service.opensandbox_proxy_service import OpenSandboxProxyService

    rock_config.runtime.operator_type = "opensandbox"

    service = create_sandbox_proxy_service(rock_config, sandbox_proxy_service._meta_store)

    assert type(service) is OpenSandboxProxyService
    assert isinstance(service, SandboxProxyService)
    await service.aclose()


def test_create_proxy_service_keeps_existing_service_for_rocklet_operators(rock_config, sandbox_proxy_service):
    from rock.sandbox.service.factory import create_sandbox_proxy_service

    rock_config.runtime.operator_type = "ray"

    service = create_sandbox_proxy_service(rock_config, sandbox_proxy_service._meta_store)

    assert type(service) is SandboxProxyService
