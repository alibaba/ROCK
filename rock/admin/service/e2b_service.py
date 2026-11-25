from typing import TYPE_CHECKING

from pydantic import ValidationError

from rock.actions.sandbox.response import State
from rock.admin.core.sandbox_table import SandboxTable
from rock.admin.core.template_table import TemplateTable
from rock.admin.proto.request import ClusterInfo, E2BColdStartOptions, UserInfo
from rock.admin.proto.response import E2BSandboxInfo, SandboxStartResponse, SandboxStatusResponse
from rock.admin.service.e2b_sandbox_info import e2b_sandbox_info_fields
from rock.admin.service.snapshot_manager import SnapshotConflict
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
        template_table: TemplateTable | None = None,
        image_resolver: "ImageResolver | None" = None,
        *,
        sandbox_table: SandboxTable | None = None,
        snapshot_service=None,
    ) -> None:
        self._sandbox_manager = sandbox_manager
        self._template_table = template_table
        self._image_resolver = image_resolver
        self._sandbox_table = sandbox_table
        self.snapshot_service = snapshot_service

    async def start(
        self,
        config: DockerDeploymentConfig,
        user_info: UserInfo = {},
        cluster_info: ClusterInfo = {},
    ) -> SandboxStartResponse:
        if self.snapshot_service is not None:
            record = await self.snapshot_service.get_start_snapshot(config.image)
            defaults = {}
            if record["kind"] == "BASE_IMAGE":
                resources = record.get("resource_spec") or {}
                if resources.get("vcpus") is not None:
                    defaults["cpus"] = resources["vcpus"]
                if resources.get("memory_mib") is not None:
                    defaults["memory"] = megabytes_to_size(resources["memory_mib"])
                if resources.get("disk_size_mb") is not None:
                    defaults["disk"] = megabytes_to_size(resources["disk_size_mb"])
            return await self._sandbox_manager.start_from_template(
                config.model_copy(update={**defaults, "template_id": None, "source_snapshot_id": record["snapshot_id"]}),
                user_info=user_info,
                cluster_info=cluster_info,
            )
        if self._template_table is None:
            raise RuntimeError("non-snapshot backends require a template table")
        template = await self._template_table.get_ready_template(config.image)
        wait_options = {}
        if template is None:
            try:
                cold_start = E2BColdStartOptions.model_validate(config.metadata)
            except ValidationError as error:
                details = "; ".join(f"{item['loc'][0]}: {item['msg']}" for item in error.errors())
                raise BadRequestRockError(f"Invalid cold-start metadata: {details}") from None
            if cold_start.startup_timeout is not None:
                wait_options["wait_timeout"] = cold_start.startup_timeout
            logger.info("Template %s is not ready or does not exist; using raw manifest", config.image)
            template_config = config.model_copy(
                update={
                    "template_id": None,
                    "cpus": cold_start.cpu_count if cold_start.cpu_count is not None else config.cpus,
                    "memory": (
                        megabytes_to_size(cold_start.memory_mb) if cold_start.memory_mb is not None else config.memory
                    ),
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
        if self._image_resolver is not None and not template_config.source_snapshot_id:
            try:
                template_config.image = await self._image_resolver.resolve(template_config.image)
            except Exception as error:
                logger.warning("Image resolution failed; keeping the original image (%s)", type(error).__name__)
        return await self._sandbox_manager.start_from_template(
            template_config,
            user_info=user_info,
            cluster_info=cluster_info,
            **wait_options,
        )

    @property
    def supports_running_delete(self) -> bool:
        return self._sandbox_manager.supports_running_delete

    async def get_sandbox(self, sandbox_id: str) -> E2BSandboxInfo:
        try:
            status = await self.get_status(sandbox_id, include_all_states=True)
        except BadRequestRockError as error:
            raise E2BSandboxNotFoundError(str(error)) from None
        try:
            fields = e2b_sandbox_info_fields(sandbox_id, status)
        except ValueError as error:
            raise E2BSandboxUnavailableError(
                f"Sandbox {sandbox_id} does not yet expose a verified E2B host and lifecycle projection"
            ) from error
        access_token = await self.get_sandbox_access_token(sandbox_id)
        if access_token is not None:
            fields["envdAccessToken"] = access_token
        return E2BSandboxInfo(**fields)

    async def get_sandbox_access_token(self, sandbox_id: str) -> str | None:
        if self._sandbox_table is None or not self._sandbox_manager.snapshot_runtime_configured:
            return None
        row = await self._sandbox_table.get(sandbox_id)
        if row is None:
            return None
        token = row.get("auth_token")
        return token if isinstance(token, str) and token else None

    async def get_status(
        self,
        sandbox_id: str,
        include_all_states: bool = False,
    ) -> SandboxStatusResponse:
        return await self._sandbox_manager.get_status(
            sandbox_id, include_all_states=include_all_states, refresh_timeout=False
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

    async def pause(self, sandbox_id: str) -> bool:
        try:
            status = await self.get_status(sandbox_id, include_all_states=True)
            if status.state == State.DELETED:
                raise E2BSandboxNotFoundError(f"Sandbox {sandbox_id} not found")
            if status.state in {State.STOPPED, State.ARCHIVED}:
                return False
            if status.state != State.RUNNING:
                raise SnapshotConflict(f"sandbox {sandbox_id} must be running to pause")
            if not self._sandbox_manager.snapshot_runtime_configured:
                raise SnapshotConflict("pause requires a snapshot-backed runtime")
            await self.stop(sandbox_id)
            return True
        except BadRequestRockError as error:
            raise E2BSandboxNotFoundError(str(error)) from None

    async def connect(self, sandbox_id: str, *, timeout: int = 300) -> bool:
        try:
            status = await self.get_status(sandbox_id, include_all_states=True)
            if status.state == State.DELETED:
                raise E2BSandboxNotFoundError(f"Sandbox {sandbox_id} not found")
            if not self._sandbox_manager.snapshot_runtime_configured:
                if status.state != State.RUNNING:
                    raise SnapshotConflict("resume requires a snapshot-backed runtime")
                await self._sandbox_manager.extend_timeout(sandbox_id, timeout)
                return False
            if status.state not in {State.RUNNING, State.STOPPED, State.PENDING}:
                raise SnapshotConflict(f"sandbox {sandbox_id} cannot be connected in state {status.state}")
            await self._sandbox_manager.restart_async(sandbox_id, timeout_seconds=timeout)
            return status.state != State.RUNNING
        except BadRequestRockError as error:
            raise E2BSandboxNotFoundError(str(error)) from None


class E2BSandboxUnavailableError(RuntimeError):
    pass
