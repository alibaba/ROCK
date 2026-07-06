import asyncio
import datetime
import time
from datetime import timezone

from fastapi import UploadFile

from rock import env_vars
from rock.actions import (
    BashObservation,
    CloseBashSessionResponse,
    CommandResponse,
    CreateBashSessionResponse,
    ReadFileResponse,
    UploadResponse,
    WriteFileResponse,
)
from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.ray_service import RayService
from rock.admin.metrics.decorator import monitor_sandbox_operation
from rock.admin.proto.request import ClusterInfo, UserInfo
from rock.admin.proto.request import SandboxAction as Action
from rock.admin.proto.request import SandboxCloseBashSessionRequest as CloseBashSessionRequest
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxCreateSessionRequest as CreateSessionRequest
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.proto.request import SandboxWriteFileRequest as WriteFileRequest
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.common.constants import DeleteReason, StopReason
from rock.config import RockConfig, RuntimeConfig
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.logger import init_logger
from rock.rocklet import __version__ as swe_version
from rock.sandbox import __version__ as gateway_version
from rock.sandbox.archive.abstract import AbstractDirStorage, AbstractImageStorage
from rock.sandbox.base_manager import BaseManager
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.sandbox_actor import SandboxActor
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.sandbox.sandbox_statemachine import SandboxStateMachine, get_current_state_started_at
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sandbox.utils.timeout import SandboxTimeoutHelper
from rock.sdk.common.exceptions import BadRequestRockError, InternalServerRockError
from rock.utils import REQUEST_TIMEOUT_SECONDS, StageTimer
from rock.utils.crypto_utils import AESEncryption
from rock.utils.format import convert_to_gb, parse_size_to_bytes
from rock.utils.system import get_iso8601_timestamp

logger = init_logger(__name__)


class SandboxManager(BaseManager):
    _ray_namespace: str = None

    def __init__(
        self,
        rock_config: RockConfig,
        meta_store: SandboxMetaStore,
        ray_namespace: str = env_vars.ROCK_RAY_NAMESPACE,
        ray_service: RayService | None = None,
        enable_runtime_auto_clear: bool = False,
        operator: AbstractOperator | None = None,
    ):
        super().__init__(
            rock_config,
            meta_store=meta_store,
            enable_runtime_auto_clear=enable_runtime_auto_clear,
        )
        self._ray_service = ray_service
        self._ray_namespace = ray_namespace
        self._operator = operator
        self._dir_storage = None
        self._image_storage = None
        self._init_archive_storage(rock_config)
        self._aes_encrypter = AESEncryption()
        self._proxy_service = SandboxProxyService(rock_config=rock_config, meta_store=meta_store)
        logger.info("sandbox service init success")

    def _init_archive_storage(self, rock_config: RockConfig) -> None:
        archive_cfg = rock_config.lifecycle.archive
        image_registry_cfg = archive_cfg.registry
        dir_storage_cfg = archive_cfg.dir_storage
        registry_ready = image_registry_cfg.registry_url and image_registry_cfg.username and image_registry_cfg.password
        dir_storage_ready = (
            dir_storage_cfg.endpoint and dir_storage_cfg.access_key_id and dir_storage_cfg.access_key_secret
        )
        if not (registry_ready and dir_storage_ready):
            logger.warning("archive storage credentials incomplete, archive/restore will be unavailable")
            return
        self._dir_storage = AbstractDirStorage.from_config(dir_storage_cfg)
        self._image_storage = AbstractImageStorage.from_config(image_registry_cfg)
        logger.info(
            f"archive storage initialized: dir_storage={dir_storage_cfg.type}://{dir_storage_cfg.bucket}, "
            f"image_storage={image_registry_cfg.registry_url}/{image_registry_cfg.namespace}"
        )

    async def _get_current_statemachine(self, sandbox_id: str) -> SandboxStateMachine | None:
        """Fetch current state from meta store and return a restored SandboxStateMachine, or None if not found."""
        info = await self._meta_store.get(sandbox_id, check_db=True)
        if info is None:
            return None
        return await SandboxStateMachine.from_state_value(info.get("state"), sandbox_info=info)

    async def refresh_aes_key(self):
        try:
            await self.rock_config.update()
            if aes_encrypt_key := self.rock_config.proxy_service.aes_encrypt_key:
                self._aes_encrypter.key_update(aes_encrypt_key)
        except Exception as e:
            logger.error(f"update aes key failed, error: {e}")
            raise InternalServerRockError(f"update aes key failed, {str(e)}")

    async def _check_sandbox_exists_in_redis(self, config: DeploymentConfig):
        if isinstance(config, DockerDeploymentConfig) and config.container_name:
            sandbox_id = config.container_name
            if await self._meta_store.exists(sandbox_id):
                raise BadRequestRockError(f"Sandbox {sandbox_id} already exists")

    def _setup_sandbox_actor_metadata(self, sandbox_actor: SandboxActor, user_info: UserInfo) -> None:
        user_id = user_info.get("user_id", "default")
        experiment_id = user_info.get("experiment_id", "default")
        namespace = user_info.get("namespace", "default")

        sandbox_actor.set_user_id.remote(user_id)
        sandbox_actor.set_experiment_id.remote(experiment_id)
        sandbox_actor.set_namespace.remote(namespace)

    async def _build_sandbox_info_metadata(
        self, sandbox_info: SandboxInfo, user_info: UserInfo, cluster_info: ClusterInfo
    ) -> None:
        sandbox_info["memory"] = convert_to_gb(sandbox_info.get("memory"))
        sandbox_info["user_id"] = user_info.get("user_id", "default")
        sandbox_info["experiment_id"] = user_info.get("experiment_id", "default")
        sandbox_info["namespace"] = user_info.get("namespace", "default")
        sandbox_info["cluster_name"] = cluster_info.get("cluster_name", "default")
        rock_auth = user_info.get("rock_authorization", "default")
        await self.refresh_aes_key()
        sandbox_info["rock_authorization_encrypted"] = self._aes_encrypter.encrypt(rock_auth)
        sandbox_info["state"] = State.PENDING
        sandbox_info["create_time"] = get_iso8601_timestamp()

    @monitor_sandbox_operation()
    async def start_async(
        self, config: DeploymentConfig, user_info: UserInfo = {}, cluster_info: ClusterInfo = {}
    ) -> SandboxStartResponse:
        await self._check_sandbox_exists_in_redis(config)
        self.validate_sandbox_spec(self.rock_config.runtime, config)
        with StageTimer("startup_timing", f"[{config.image}] Init config", logger):
            docker_deployment_config: DockerDeploymentConfig = await self.deployment_manager.init_config(config)

        sandbox_id = docker_deployment_config.container_name
        if self.rock_config.runtime.use_standard_spec_only:
            logger.info(
                f"[{sandbox_id}] Using standard spec only: "
                f"cpus={self.rock_config.runtime.standard_spec.cpus}, "
                f"memory={self.rock_config.runtime.standard_spec.memory}"
            )
            docker_deployment_config.cpus = self.rock_config.runtime.standard_spec.cpus
            docker_deployment_config.memory = self.rock_config.runtime.standard_spec.memory
        with StageTimer("startup_timing", f"[{sandbox_id}] Operator submit", logger):
            sandbox_info: SandboxInfo = await self._operator.submit(docker_deployment_config, user_info)
        await self._build_sandbox_info_metadata(sandbox_info, user_info, cluster_info)
        timeout_info = SandboxTimeoutHelper.make_timeout_info(docker_deployment_config.auto_clear_time)
        with StageTimer("startup_timing", f"[{sandbox_id}] Meta store create", logger):
            await self._meta_store.create(
                sandbox_id,
                sandbox_info,
                timeout_info=timeout_info,
                deployment_config=docker_deployment_config,
            )
        return SandboxStartResponse(
            sandbox_id=sandbox_id,
            host_name=sandbox_info.get("host_name"),
            host_ip=sandbox_info.get("host_ip"),
        )

    @monitor_sandbox_operation()
    async def restart_async(self, sandbox_id: str) -> SandboxStartResponse:
        sm = await self._get_current_statemachine(sandbox_id)
        if sm is None:
            raise BadRequestRockError(f"Sandbox {sandbox_id} not found")

        state = sm.current_state.value
        if state == State.ARCHIVED:
            if not self._dir_storage or not self._image_storage:
                raise BadRequestRockError("archive not configured: missing storage credentials")
            await sm.send(
                "restore",
                sandbox_id=sandbox_id,
                meta_store=self._meta_store,
                operator=self._operator,
                dir_storage=self._dir_storage,
                image_storage=self._image_storage,
                restore_timeout_seconds=self.rock_config.lifecycle.archive.restore_timeout_seconds,
            )
        elif state == State.STOPPED:
            await sm.send(
                "restart",
                sandbox_id=sandbox_id,
                operator=self._operator,
                meta_store=self._meta_store,
            )
        else:
            raise BadRequestRockError(f"Sandbox {sandbox_id} cannot be restarted: current state is '{state.value}'")

        info: SandboxInfo = sm.sandbox_info or {}
        return SandboxStartResponse(
            sandbox_id=sandbox_id,
            host_name=info.get("host_name"),
            host_ip=info.get("host_ip"),
        )

    @monitor_sandbox_operation()
    async def start(self, config: DeploymentConfig) -> SandboxStartResponse:
        response = await self.start_async(config)
        sandbox_id = response.sandbox_id
        deadline = time.time() + REQUEST_TIMEOUT_SECONDS
        with StageTimer("startup_timing", f"[{sandbox_id}] Wait sandbox running", logger):
            while True:
                status = await self.get_status(sandbox_id)
                if status.is_alive:
                    break
                if time.time() >= deadline:
                    raise TimeoutError(f"sandbox {sandbox_id} not running after {REQUEST_TIMEOUT_SECONDS}s")
                await asyncio.sleep(1)
        return response

    @monitor_sandbox_operation()
    async def stop(self, sandbox_id: str, reason: StopReason = StopReason.MANUAL):
        sm = await self._get_current_statemachine(sandbox_id)
        if sm is None:
            logger.info(f"stop dangling sandbox {sandbox_id}")
            try:
                await self._operator.stop(sandbox_id, reason=reason)
            except ValueError as e:
                logger.error(f"ray get actor, actor {sandbox_id} not exist", exc_info=e)
        elif sm.current_state.value == State.STOPPED:
            await sm.send("stop_noop", sandbox_id=sandbox_id)
        else:
            await sm.send(
                "stop",
                sandbox_id=sandbox_id,
                operator=self._operator,
                meta_store=self._meta_store,
                reason=reason,
            )
            # `--rm` containers are already gone after stop; cascade to DELETED
            # so the metadata row doesn't linger in STOPPED.
            # Redis keys are gone after archive; re-read from DB to get spec.
            sm = await self._get_current_statemachine(sandbox_id)
            spec = ((sm.sandbox_info or {}).get("spec") or {}) if sm else {}
            if spec.get("remove_container"):
                await sm.send(
                    "delete",
                    sandbox_id=sandbox_id,
                    operator=self._operator,
                    meta_store=self._meta_store,
                    reason=DeleteReason.IMMEDIATE,
                )

    @monitor_sandbox_operation()
    async def delete(self, sandbox_id: str, reason: DeleteReason = DeleteReason.MANUAL) -> None:
        sm = await self._get_current_statemachine(sandbox_id)
        if sm is None:
            logger.info(f"delete: sandbox {sandbox_id} not found, noop")
            return
        state = sm.current_state.value
        if state == State.DELETED:
            logger.info(f"delete: sandbox {sandbox_id} already deleted, noop")
            return
        if state not in (State.STOPPED, State.ARCHIVED):
            raise BadRequestRockError(
                f"Sandbox {sandbox_id} cannot be deleted: current state is '{state.value}', must be stopped or archived first"
            )

        await sm.send(
            "delete",
            sandbox_id=sandbox_id,
            operator=self._operator,
            meta_store=self._meta_store,
            reason=reason,
            dir_storage=self._dir_storage,
            image_storage=self._image_storage,
        )

    async def get_mount(self, sandbox_id):
        async with self._ray_service.get_ray_rwlock().read_lock():
            actor_name = self.deployment_manager.get_actor_name(sandbox_id)
            sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
            if sandbox_actor is None:
                await self._meta_store.archive(sandbox_id, {})
                raise Exception(f"sandbox {sandbox_id} not found to get mount")
            result = await self._ray_service.async_ray_get(sandbox_actor.get_mount.remote())
            logger.info(f"get_mount: {result}")
            return result

    @monitor_sandbox_operation()
    async def commit(self, sandbox_id, image_tag: str, username: str, password: str) -> CommandResponse:
        async with self._ray_service.get_ray_rwlock().read_lock():
            logger.info(f"commit sandbox {sandbox_id}")
            actor_name = self.deployment_manager.get_actor_name(sandbox_id)
            sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
            if sandbox_actor is None:
                await self._meta_store.archive(sandbox_id, {})
                raise Exception(f"sandbox {sandbox_id} not found to commit")
            logger.info(f"begin to commit {sandbox_id} to {image_tag}")
            result = await self._ray_service.async_ray_get(sandbox_actor.commit.remote(image_tag, username, password))
            logger.info(f"commit {sandbox_id} to {image_tag} finished, result {result}")
            return result

    async def _try_advance_pending(self, sandbox_id: str, sm) -> dict | None:
        """Probe operator alive; fire ``alive`` transition if RUNNING. Returns operator info or None."""
        operator_sandbox_info = await self._operator.get_status(sandbox_id=sandbox_id)
        if operator_sandbox_info is None:
            return None
        is_alive = operator_sandbox_info.get("state") == State.RUNNING
        if sm.current_state.value == State.PENDING and is_alive:
            await sm.send(
                "alive", sandbox_id=sandbox_id, meta_store=self._meta_store, sandbox_info=operator_sandbox_info
            )
        return operator_sandbox_info

    @monitor_sandbox_operation()
    async def get_status(self, sandbox_id, include_all_states: bool = False) -> SandboxStatusResponse:
        sandbox_info = await self._meta_store.get(sandbox_id, check_db=True)
        if sandbox_info is None:
            raise BadRequestRockError(f"Sandbox {sandbox_id} not found")

        state = sandbox_info.get("state")
        is_alive = False
        operator_sandbox_info: SandboxInfo | None = await self._operator.get_status(sandbox_id=sandbox_id)
        if operator_sandbox_info is not None:
            is_alive = operator_sandbox_info.get("state") == State.RUNNING

            if is_alive and state == State.PENDING:
                sm = await self._get_current_statemachine(sandbox_id)
                await sm.send(
                    "alive", sandbox_id=sandbox_id, meta_store=self._meta_store, sandbox_info=operator_sandbox_info
                )

            if operator_sandbox_info.get("state") in (State.PENDING, State.RUNNING):
                await self._refresh_timeout(sandbox_id)

        if state == State.ARCHIVING:
            sm = await self._get_current_statemachine(sandbox_id)
            if sm:
                await self._try_advance_archiving(sandbox_id, sm)

        # compat with legacy get_status behavior by default (include_all_states == False),
        # raise 'not found' if not on pending or running status.
        if not include_all_states and sandbox_info.get("state") not in (State.PENDING, State.RUNNING):
            raise BadRequestRockError(f"Sandbox {sandbox_id} not found")

        if operator_sandbox_info is not None:
            sandbox_info = operator_sandbox_info

        return SandboxStatusResponse(
            sandbox_id=sandbox_id,
            status=sandbox_info.get("phases"),
            port_mapping=sandbox_info.get("port_mapping"),
            state=sandbox_info.get("state"),
            host_name=sandbox_info.get("host_name"),
            host_ip=sandbox_info.get("host_ip"),
            is_alive=is_alive,
            image=sandbox_info.get("image"),
            swe_rex_version=swe_version,
            gateway_version=gateway_version,
            user_id=sandbox_info.get("user_id"),
            experiment_id=sandbox_info.get("experiment_id"),
            namespace=sandbox_info.get("namespace"),
            cpus=sandbox_info.get("cpus"),
            memory=sandbox_info.get("memory"),
            disk=sandbox_info.get("disk"),
            disk_limit_rootfs=sandbox_info.get("disk"),
            start_time=sandbox_info.get("start_time"),
            stop_time=sandbox_info.get("stop_time"),
            create_time=sandbox_info.get("create_time"),
            state_history=sandbox_info.get("state_history", []),
        )

    async def build_sandbox_info_from_redis(self, sandbox_id: str, deployment_info: SandboxInfo) -> SandboxInfo | None:
        sandbox_info_from_store = await self._meta_store.get(sandbox_id)
        if sandbox_info_from_store:
            sandbox_info = sandbox_info_from_store
            remote_info = {
                k: v for k, v in deployment_info.items() if k in ["phases", "port_mapping", "alive", "state"]
            }
            if "phases" in remote_info and remote_info["phases"]:
                remote_info["phases"] = {name: phase.to_dict() for name, phase in remote_info["phases"].items()}
            sandbox_info.update(remote_info)
        else:
            sandbox_info = deployment_info
        return sandbox_info

    async def create_session(self, request: CreateSessionRequest) -> CreateBashSessionResponse:
        return await self._proxy_service.create_session(request)

    @monitor_sandbox_operation()
    async def run_in_session(self, action: Action) -> BashObservation:
        return await self._proxy_service.run_in_session(action)

    async def close_session(self, request: CloseBashSessionRequest) -> CloseBashSessionResponse:
        return await self._proxy_service.close_session(request)

    async def execute(self, command: Command) -> CommandResponse:
        return await self._proxy_service.execute(command)

    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        return await self._proxy_service.read_file(request)

    @monitor_sandbox_operation()
    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        return await self._proxy_service.write_file(request)

    @monitor_sandbox_operation()
    async def upload(self, file: UploadFile, target_path: str, sandbox_id: str) -> UploadResponse:
        return await self._proxy_service.upload(file, target_path, sandbox_id)

    async def _refresh_timeout(self, sandbox_id: str) -> None:
        timeout_info = await self._meta_store.get_timeout(sandbox_id)
        if timeout_info is None:
            logger.warning("refresh_timeout: timeout key not found for sandbox_id=%s", sandbox_id)
            return
        new_timeout = SandboxTimeoutHelper.refresh_timeout(timeout_info)
        if new_timeout is None:
            logger.warning("refresh_timeout: auto_clear_time missing for sandbox_id=%s", sandbox_id)
            return
        await self._meta_store.update_timeout(sandbox_id, new_timeout)

    async def _is_expired(self, sandbox_id: str) -> bool:
        timeout_info = await self._meta_store.get_timeout(sandbox_id)
        if timeout_info is None:
            logger.warning("is_expired: timeout key not found for sandbox_id=%s", sandbox_id)
            return False
        return SandboxTimeoutHelper.is_expired(timeout_info)

    async def _is_actor_alive(self, sandbox_id):
        try:
            actor_name = self.deployment_manager.get_actor_name(sandbox_id)
            actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
            return actor is not None
        except Exception as e:
            logger.error("get actor failed", exc_info=e)
            return False

    async def _auto_transition(self):
        """Long-interval scan: expire RUNNING/PENDING → STOPPED, auto-delete/archive stale STOPPED."""
        logger.info("[auto_transition] start")
        await self._auto_stop_expired()
        await self._auto_delete_stopped()
        await self._auto_archive_stopped()
        logger.info("[auto_transition] done")

    async def _auto_stop_expired(self) -> None:
        """Stop alive sandboxes that have exceeded their auto_clear timeout."""
        alive_count = 0
        expired_count = 0
        async for sandbox_id in self._meta_store.iter_alive_sandbox_ids():
            alive_count += 1
            try:
                if await self._is_expired(sandbox_id):
                    expired_count += 1
                    logger.info(f"[auto_stop] {sandbox_id} expired, stopping")
                    asyncio.create_task(self.stop(sandbox_id, reason=StopReason.EXPIRED))
                else:
                    logger.info(f"[auto_stop] {sandbox_id} not expired, skip")
            except asyncio.CancelledError:
                continue
            except Exception as e:
                logger.error(f"[auto_stop] {sandbox_id}: {e}", exc_info=True)
                continue
        logger.info(f"[auto_stop] done: alive={alive_count}, expired={expired_count}")

    async def _auto_delete_stopped(self) -> None:
        """Delete STOPPED and ARCHIVED sandboxes idle longer than auto_delete_seconds."""
        auto_delete_sec = self.rock_config.lifecycle.auto_transition.auto_delete_seconds
        if not auto_delete_sec:
            logger.info("[auto_delete] disabled (auto_delete_seconds=0)")
            return

        try:
            candidates = await self._meta_store.list_by_in(
                "state", [State.STOPPED.value, State.ARCHIVED.value], order_by="stop_time", limit=1000
            )
        except Exception as e:
            logger.warning(f"[auto_delete] list_by_in failed: {e}")
            return

        logger.info(f"[auto_delete] candidates={len(candidates)}, threshold={auto_delete_sec}s")
        now = datetime.datetime.now(timezone.utc)
        deleted_count = 0
        for info in candidates:
            sandbox_id = info.get("sandbox_id", "")
            stop_time_str = info.get("stop_time", "")
            if not sandbox_id or not stop_time_str:
                continue
            try:
                stop_time = datetime.datetime.fromisoformat(stop_time_str.replace("Z", "+00:00"))
                elapsed = (now - stop_time).total_seconds()
            except (ValueError, TypeError):
                continue
            if elapsed < auto_delete_sec:
                logger.info(
                    f"[auto_delete] {sandbox_id} (state={info.get('state')}) idle {int(elapsed)}s < {auto_delete_sec}s, skip"
                )
                continue
            try:
                logger.info(
                    f"[auto_delete] {sandbox_id} (state={info.get('state')}) idle for {int(elapsed)}s, deleting"
                )
                await self.delete(sandbox_id, reason=DeleteReason.EXPIRED)
                deleted_count += 1
            except Exception as e:
                logger.error(f"[auto_delete] {sandbox_id}: {e}", exc_info=True)
        logger.info(f"[auto_delete] done: deleted={deleted_count}/{len(candidates)}")

    async def _auto_archive_stopped(self) -> None:
        """Archive STOPPED sandboxes that have been idle longer than auto_archive_seconds."""
        auto_archive_sec = self.rock_config.lifecycle.auto_transition.auto_archive_seconds
        if not auto_archive_sec:
            logger.info("[auto_archive] disabled (auto_archive_seconds=0)")
            return
        if not self._operator or not self._dir_storage or not self._image_storage:
            logger.info("[auto_archive] skipped (storage not configured)")
            return

        try:
            stopped_list = await self._meta_store.list_by(
                "state", State.STOPPED.value, order_by="stop_time", limit=1000
            )
        except Exception as e:
            logger.warning(f"[auto_archive] list_by failed: {e}")
            return

        logger.info(f"[auto_archive] candidates={len(stopped_list)}, threshold={auto_archive_sec}s")
        now = datetime.datetime.now(timezone.utc)
        archived_count = 0
        for info in stopped_list:
            sandbox_id = info.get("sandbox_id", "")
            stop_time_str = info.get("stop_time", "")
            if not sandbox_id or not stop_time_str:
                continue
            try:
                stop_time = datetime.datetime.fromisoformat(stop_time_str.replace("Z", "+00:00"))
                elapsed = (now - stop_time).total_seconds()
            except (ValueError, TypeError):
                continue
            if elapsed < auto_archive_sec:
                logger.info(f"[auto_archive] {sandbox_id} idle {int(elapsed)}s < {auto_archive_sec}s, skip")
                continue
            try:
                logger.info(f"[auto_archive] {sandbox_id} stopped for {int(elapsed)}s, archiving")
                await self.archive_sandbox(sandbox_id)
                archived_count += 1
            except Exception as e:
                logger.error(f"[auto_archive] {sandbox_id}: {e}", exc_info=True)
        logger.info(f"[auto_archive] done: archived={archived_count}/{len(stopped_list)}")

    async def _reconcile(self) -> None:
        """Reconcile intermediate states (PENDING, ARCHIVING) on short interval."""
        logger.info("reconcile")
        await self._reconcile_pending()
        await self._reconcile_archiving()

    async def get_sandbox_statistics(self, sandbox_id):
        actor_name = self.deployment_manager.get_actor_name(sandbox_id)
        sandbox_actor = await self._ray_service.async_ray_get_actor(actor_name, self._ray_namespace)
        resource_metrics = await self._ray_service.async_ray_get(sandbox_actor.get_sandbox_statistics.remote())
        return resource_metrics

    def validate_sandbox_spec(self, runtime_config: RuntimeConfig, deployment_config: DeploymentConfig) -> None:
        try:
            memory = parse_size_to_bytes(deployment_config.memory)
            max_memory = parse_size_to_bytes(runtime_config.max_allowed_spec.memory)
            if deployment_config.cpus > runtime_config.max_allowed_spec.cpus:
                raise BadRequestRockError(
                    f"Requested CPUs {deployment_config.cpus} exceed the maximum allowed {runtime_config.max_allowed_spec.cpus}"
                )
            if memory > max_memory:
                raise BadRequestRockError(
                    f"Requested memory {deployment_config.memory} exceed the maximum allowed {runtime_config.max_allowed_spec.memory}"
                )
        except ValueError as e:
            logger.warning(f"Invalid memory size: {deployment_config.memory}", exc_info=e)
            raise BadRequestRockError(f"Invalid memory size: {deployment_config.memory}")

        # Validate disk format and cap
        if deployment_config.disk is not None:
            try:
                disk_bytes = parse_size_to_bytes(deployment_config.disk)
            except ValueError as e:
                logger.warning(f"Invalid disk size: {deployment_config.disk}", exc_info=e)
                raise BadRequestRockError(f"Invalid disk size: {deployment_config.disk}")
            if runtime_config.max_allowed_spec.disk is not None:
                max_disk_bytes = parse_size_to_bytes(runtime_config.max_allowed_spec.disk)
                if disk_bytes > max_disk_bytes:
                    raise BadRequestRockError(
                        f"Requested disk {deployment_config.disk} exceeds the maximum allowed {runtime_config.max_allowed_spec.disk}"
                    )

    async def archive_sandbox(self, sandbox_id: str) -> None:
        """Validate preconditions, then fire archive transition (cleanup + actor dispatch in on_archive)."""
        if not self._operator:
            raise BadRequestRockError("archive not supported: no operator configured")
        if not self._dir_storage or not self._image_storage:
            raise BadRequestRockError("archive not configured: missing storage credentials")

        sm = await self._get_current_statemachine(sandbox_id)
        if sm is None:
            raise BadRequestRockError(f"sandbox {sandbox_id} not found")

        archive_cfg = self.rock_config.lifecycle.archive
        archive_params = {
            "archive_prefix": archive_cfg.dir_storage.prefix,
            "registry_namespace": archive_cfg.registry.namespace,
            "max_image_push_size": archive_cfg.max_image_push_size,
            "max_dir_upload_size": archive_cfg.max_dir_upload_size,
            "timeout_seconds": self.rock_config.lifecycle.archive.archive_timeout_seconds,
        }
        await sm.send(
            "archive",
            sandbox_id=sandbox_id,
            meta_store=self._meta_store,
            operator=self._operator,
            dir_storage=self._dir_storage,
            image_storage=self._image_storage,
            archive_params=archive_params,
        )

    async def _try_advance_archiving(self, sandbox_id: str, sm: SandboxStateMachine) -> None:
        """If sandbox is ARCHIVING and service_status shows completion, transition to ARCHIVED."""
        if sm.current_state.value != State.ARCHIVING:
            return

        info = sm.sandbox_info
        host_ip = info.get("host_ip")
        if not host_ip:
            return

        try:
            remote_status = await self._operator.get_remote_status(sandbox_id, host_ip)
        except Exception as e:
            logger.warning(f"get_remote_status failed for {sandbox_id}: {e}")
            return

        phases = remote_status.to_dict().get("phases", {})
        image_phase = phases.get("image_archive", {})
        status = image_phase.get("status", "")

        if status == "success":
            try:
                await sm.send("archive_done", sandbox_id=sandbox_id, meta_store=self._meta_store)
                logger.info(f"archive_done: {sandbox_id}")
            except Exception as e:
                logger.error(f"archive_done transition failed for {sandbox_id}: {e}", exc_info=True)
        elif status in ("failed", "timeout"):
            try:
                await sm.send(
                    "archive_failed",
                    sandbox_id=sandbox_id,
                    meta_store=self._meta_store,
                    reason=image_phase.get("message", "unknown"),
                )
                logger.warning(f"archive_failed: {sandbox_id} ({image_phase.get('message')})")
            except Exception as e:
                logger.error(f"archive_failed transition failed for {sandbox_id}: {e}", exc_info=True)

    async def _reconcile_archiving(self) -> None:
        """Reconcile ARCHIVING sandboxes: verify completion or roll back to STOPPED on timeout."""
        try:
            archiving = await self._meta_store.list_by("state", State.ARCHIVING.value)
        except Exception as e:
            logger.warning(f"[reconcile_archiving] list_by failed: {e}")
            return

        logger.info(f"[reconcile_archiving] found {len(archiving)} ARCHIVING sandboxes")
        for info in archiving:
            sandbox_id = info.get("sandbox_id", "")
            if not sandbox_id:
                continue

            sm = await self._get_current_statemachine(sandbox_id)
            if not sm or sm.current_state.value != State.ARCHIVING:
                logger.info(f"[reconcile_archiving] {sandbox_id} state mismatch, skip")
                continue

            await self._try_advance_archiving(sandbox_id, sm)
            if sm.current_state.value != State.ARCHIVING:
                logger.info(f"[reconcile_archiving] {sandbox_id} advanced to {sm.current_state.value.value}")
                continue

            started_at = get_current_state_started_at(info.get("state_history", []), "archiving")
            if not started_at:
                continue
            try:
                started = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                elapsed = (datetime.datetime.now(timezone.utc) - started).total_seconds()
            except (ValueError, TypeError):
                continue

            timeout_sec = self.rock_config.lifecycle.archive.archive_timeout_seconds
            if elapsed < timeout_sec:
                logger.info(f"[reconcile_archiving] {sandbox_id} in progress {int(elapsed)}s < {timeout_sec}s")
                continue

            try:
                await sm.send(
                    "archive_failed",
                    sandbox_id=sandbox_id,
                    meta_store=self._meta_store,
                    reason=f"timeout after {int(elapsed)}s",
                )
                logger.warning(f"[reconcile_archiving] {sandbox_id} timed out after {int(elapsed)}s, rolled back")
            except Exception as e:
                logger.error(
                    f"[reconcile_archiving] archive_failed transition failed for {sandbox_id}: {e}", exc_info=True
                )

    async def _reconcile_pending(self) -> None:
        """Reconcile PENDING sandboxes: advance to RUNNING or timeout restore back to ARCHIVED."""
        try:
            pending_list = await self._meta_store.list_by("state", State.PENDING.value)
        except Exception as e:
            logger.warning(f"[reconcile_pending] list_by failed: {e}")
            return

        logger.info(f"[reconcile_pending] found {len(pending_list)} PENDING sandboxes")
        for info in pending_list:
            sandbox_id = info.get("sandbox_id", "")
            if not sandbox_id:
                continue
            try:
                # 1. Restore timeout (archive_time present = restoring from ARCHIVED)
                started_at = get_current_state_started_at(info.get("state_history", []), "pending")
                if info.get("archive_time") and started_at:
                    try:
                        started = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                        elapsed = (datetime.datetime.now(timezone.utc) - started).total_seconds()
                    except (ValueError, TypeError):
                        elapsed = 0
                    timeout_sec = self.rock_config.lifecycle.archive.restore_timeout_seconds
                    if elapsed >= timeout_sec:
                        sm = await self._get_current_statemachine(sandbox_id)
                        if sm and sm.current_state.value == State.PENDING:
                            await sm.send(
                                "restore_failed",
                                sandbox_id=sandbox_id,
                                meta_store=self._meta_store,
                                reason=f"timeout after {int(elapsed)}s",
                            )
                            logger.warning(f"[reconcile_pending] restore_failed: {sandbox_id} ({int(elapsed)}s)")
                        continue
                    logger.info(f"[reconcile_pending] {sandbox_id} restoring {int(elapsed)}s < {timeout_sec}s")
                    continue

                # 2. Try advance PENDING → RUNNING
                sm = await self._get_current_statemachine(sandbox_id)
                if sm is None:
                    continue
                logger.info(f"[reconcile_pending] {sandbox_id} checking alive")
                await self._try_advance_pending(sandbox_id, sm)

            except asyncio.CancelledError:
                continue
            except Exception as e:
                logger.error(f"[reconcile_pending] {sandbox_id}: {e}", exc_info=True)
                continue
