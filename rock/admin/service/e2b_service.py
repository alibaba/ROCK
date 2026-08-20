from rock.admin.core.template_table import TemplateTable
from rock.admin.proto.request import ClusterInfo, UserInfo
from rock.admin.proto.response import E2BSandboxInfo, SandboxStartResponse, SandboxStatusResponse
from rock.admin.service.e2b_sandbox_info import e2b_sandbox_info_fields
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError, E2BSandboxNotFoundError

_MEGABYTES_PER_GIGABYTE = 1024


def _e2b_megabytes_to_rock_size(megabytes: int) -> str:
    if megabytes % _MEGABYTES_PER_GIGABYTE == 0:
        return f"{megabytes // _MEGABYTES_PER_GIGABYTE}g"
    return f"{megabytes}m"


class E2BService:
    def __init__(
        self,
        sandbox_manager: SandboxManager,
        template_table: TemplateTable,
        *,
        resolve_template_image: bool,
    ) -> None:
        self._sandbox_manager = sandbox_manager
        self._template_table = template_table
        self._resolve_template_image = resolve_template_image

    async def start(
        self,
        config: DockerDeploymentConfig,
        user_info: UserInfo = {},
        cluster_info: ClusterInfo = {},
    ) -> SandboxStartResponse:
        template = await self._template_table.get_ready_template(config.image)
        if template is None:
            raise BadRequestRockError(f"Template {config.image} is not ready or does not exist")

        updates = {
            "cpus": template["cpu_count"],
            "memory": _e2b_megabytes_to_rock_size(template["memory_mb"]),
            "disk": _e2b_megabytes_to_rock_size(template["disk_size_mb"]),
        }
        if self._resolve_template_image:
            if template["image"] is None:
                raise BadRequestRockError(f"Template {config.image} has no image")
            updates["image"] = template["image"]
        template_config = config.model_copy(
            update=updates,
        )
        return await self._sandbox_manager.start_from_template(
            template_config,
            user_info=user_info,
            cluster_info=cluster_info,
        )

    @property
    def supports_running_delete(self) -> bool:
        return self._sandbox_manager.supports_running_delete

    async def get_sandbox(self, sandbox_id: str) -> E2BSandboxInfo:
        try:
            status = await self._sandbox_manager.get_status(sandbox_id, include_all_states=True)
        except BadRequestRockError as error:
            raise E2BSandboxNotFoundError(str(error)) from None
        return E2BSandboxInfo(**e2b_sandbox_info_fields(sandbox_id, status))

    async def get_status(self, sandbox_id: str, include_all_states: bool = False) -> SandboxStatusResponse:
        return await self._sandbox_manager.get_status(sandbox_id, include_all_states=include_all_states)

    async def stop(self, sandbox_id: str) -> None:
        await self._sandbox_manager.stop(sandbox_id)

    async def delete(self, sandbox_id: str) -> None:
        await self._sandbox_manager.delete(sandbox_id)
