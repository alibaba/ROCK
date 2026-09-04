from typing import TYPE_CHECKING

from rock.admin.core.template_table import TemplateTable
from rock.admin.proto.request import ClusterInfo, UserInfo
from rock.admin.proto.response import E2BSandboxInfo, SandboxStartResponse, SandboxStatusResponse
from rock.admin.service.e2b_sandbox_info import e2b_sandbox_info_fields
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.operator.remote.constants import EXT_USE_RAW, EXT_USE_RAW_ENABLED
from rock.sandbox.sandbox_manager import SandboxManager
from rock.sdk.common.exceptions import BadRequestRockError, E2BSandboxNotFoundError, SandboxNotFoundRockError
from rock.utils.format import megabytes_to_size

if TYPE_CHECKING:
    from rock.admin.service.image_resolver import ImageResolver

logger = init_logger(__name__)


class E2BService:
    def __init__(
        self,
        sandbox_manager: SandboxManager,
        template_table: TemplateTable,
        image_resolver: "ImageResolver | None" = None,
    ) -> None:
        self._sandbox_manager = sandbox_manager
        self._template_table = template_table
        self._image_resolver = image_resolver

    async def start(
        self,
        config: DockerDeploymentConfig,
        user_info: UserInfo = {},
        cluster_info: ClusterInfo = {},
    ) -> SandboxStartResponse:
        template = await self._template_table.get_ready_template(config.image)
        if template is None:
            logger.info("Template %s is not ready or does not exist; using raw manifest", config.image)
            template_config = config.model_copy(
                update={
                    "template_id": None,
                    "extended_params": {**config.extended_params, EXT_USE_RAW: EXT_USE_RAW_ENABLED},
                }
            )
        else:
            template_config = config.model_copy(
                update={
                    "image": template["image"] or config.image,
                    "cpus": template["cpu_count"],
                    "memory": megabytes_to_size(template["memory_mb"]),
                    "disk": megabytes_to_size(template["disk_size_mb"]),
                }
            )
        if self._image_resolver is not None:
            try:
                template_config.image = await self._image_resolver.resolve(template_config.image)
            except Exception as error:
                logger.warning("Image resolution failed; keeping the original image (%s)", type(error).__name__)
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
            status = await self._sandbox_manager.get_status(
                sandbox_id,
                include_all_states=True,
                refresh_timeout=False,
            )
        except BadRequestRockError as error:
            raise E2BSandboxNotFoundError(str(error)) from None
        return E2BSandboxInfo(**e2b_sandbox_info_fields(sandbox_id, status))

    async def get_status(
        self,
        sandbox_id: str,
        include_all_states: bool = False,
    ) -> SandboxStatusResponse:
        return await self._sandbox_manager.get_status(
            sandbox_id,
            include_all_states=include_all_states,
            refresh_timeout=False,
        )

    async def set_timeout(self, sandbox_id: str, timeout_seconds: int) -> None:
        try:
            await self._sandbox_manager.set_timeout(sandbox_id, timeout_seconds)
        except SandboxNotFoundRockError as error:
            raise E2BSandboxNotFoundError(str(error)) from None

    async def stop(self, sandbox_id: str) -> None:
        await self._sandbox_manager.stop(sandbox_id)

    async def delete(self, sandbox_id: str) -> None:
        await self._sandbox_manager.delete(sandbox_id)
