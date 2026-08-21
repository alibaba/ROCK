from rock.admin.core.template_table import TemplateTable
from rock.admin.proto.request import ClusterInfo, UserInfo
from rock.admin.proto.response import E2BSandboxInfo, SandboxStartResponse, SandboxStatusResponse
from rock.admin.service.e2b_sandbox_info import e2b_sandbox_info_fields
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError, E2BSandboxNotFoundError
from rock.utils.format import megabytes_to_size

logger = init_logger(__name__)


class E2BService:
    def __init__(self, sandbox_manager: SandboxManager, template_table: TemplateTable) -> None:
        self._sandbox_manager = sandbox_manager
        self._template_table = template_table

    async def start(
        self,
        config: DockerDeploymentConfig,
        user_info: UserInfo = {},
        cluster_info: ClusterInfo = {},
    ) -> SandboxStartResponse:
        template = await self._template_table.get_ready_template(config.image)
        if template is None:
            logger.info("Template %s is not ready or does not exist; using request config", config.image)
            template_config = config
        else:
            template_config = config.model_copy(
                update={
                    "image": template["image"] or config.image,
                    "cpus": template["cpu_count"],
                    "memory": megabytes_to_size(template["memory_mb"]),
                    "disk": megabytes_to_size(template["disk_size_mb"]),
                }
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
