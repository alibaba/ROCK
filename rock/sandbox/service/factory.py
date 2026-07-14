from rock.config import RockConfig
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.sandbox.service.opensandbox_proxy_service import OpenSandboxProxyService
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService


def create_sandbox_proxy_service(
    rock_config: RockConfig,
    meta_store: SandboxMetaStore,
) -> SandboxProxyService:
    if rock_config.runtime.operator_type.lower() == "opensandbox":
        return OpenSandboxProxyService(rock_config=rock_config, meta_store=meta_store)
    return SandboxProxyService(rock_config=rock_config, meta_store=meta_store)
