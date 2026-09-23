import asyncio
import datetime
import json
import math
import secrets
import time
import uuid
import zoneinfo
from dataclasses import replace
from datetime import timezone
from typing import TYPE_CHECKING, Any

from fastapi import UploadFile
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from rock import InternalServerRockError, env_vars
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
from rock.actions.sandbox.sandbox_info import SandboxInfo, is_missing_host_ip
from rock.admin.core.ray_service import RayService
from rock.admin.core.sandbox_table import is_sandbox_identity_conflict
from rock.admin.metrics.decorator import monitor_sandbox_operation
from rock.admin.proto.request import ClusterInfo, UserInfo
from rock.admin.proto.request import SandboxAction as Action
from rock.admin.proto.request import SandboxCloseBashSessionRequest as CloseBashSessionRequest
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxCreateSessionRequest as CreateSessionRequest
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.proto.request import SandboxWriteFileRequest as WriteFileRequest
from rock.admin.proto.response import SandboxStartResponse, SandboxStatusResponse
from rock.admin.proto.snapshot import (
    CreateBaseImageRequest,
    CreateSnapshotRequest,
    ForkSnapshotRequest,
    StartFromSnapshotRequest,
)
from rock.admin.snapd.client import SnapdResultUnknown
from rock.admin.snapd.protocol import GuestEndpoint, RockletArtifactIdentity
from rock.common.constants import DeleteReason, StopReason
from rock.config import RockConfig, RuntimeConfig
from rock.deployments.config import DeploymentConfig, DockerDeploymentConfig
from rock.deployments.constants import Port
from rock.deployments.start_config import apply_start_config
from rock.logger import init_logger
from rock.rocklet import __version__ as swe_version
from rock.sandbox import __version__ as gateway_version
from rock.sandbox.archive.abstract import AbstractDirStorage, AbstractImageStorage
from rock.sandbox.base_manager import BaseManager
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.snapd import (
    OperatorLifecycleError,
    OperatorResultCode,
    OperatorSandboxResult,
    OperatorTarget,
    SnapdOperator,
    SnapdPlacement,
    SnapdSandboxRuntime,
    operator_result_from_info,
)
from rock.sandbox.sandbox_actor import SandboxActor
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.sandbox.sandbox_statemachine import (
    SandboxLifecycleHelper,
    SandboxStateMachine,
)
from rock.sandbox.service.factory import create_sandbox_proxy_service
from rock.sandbox.snapshot_types import (
    ActiveSnapshotUser,
    ForkChildLaunchPlan,
    ForkChildOutcome,
    ForkExecutionResult,
    LaunchableSnapshot,
    SandboxProductView,
    SnapshotSource,
)
from rock.sandbox.utils.timeout import SandboxTimeoutHelper
from rock.sdk.common.exceptions import BadRequestRockError, SandboxNotFoundRockError
from rock.utils import REQUEST_TIMEOUT_SECONDS, StageTimer
from rock.utils.crypto_utils import AESEncryption
from rock.utils.format import convert_to_gb, parse_size_to_bytes
from rock.utils.system import get_iso8601_timestamp

if TYPE_CHECKING:
    from rock.admin.service.snapshot_manager import SnapshotManager

logger = init_logger(__name__)

# Bound batch-wide fan-out to protect the Ray control plane.
_AUTO_DELETE_CONCURRENCY = 50
_SNAPSHOT_REFERENCE_TERMINAL_STATES = frozenset({State.STOPPED.value, State.ARCHIVED.value, State.DELETED.value})


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
        self._snapshot_manager: SnapshotManager | None = None
        self._snapshot_template_start_timeout_seconds = 60.0
        self._dir_storage = None
        self._image_storage = None
        self._init_archive_storage(rock_config)
        aes_encrypt_key = rock_config.aes_encrypt_key
        if not aes_encrypt_key:
            raise ValueError("aes_encrypt_key must be configured in YAML for the admin role")
        self._aes_encrypter = AESEncryption(aes_encrypt_key)
        self._proxy_service = create_sandbox_proxy_service(rock_config=rock_config, meta_store=meta_store)
        logger.info("sandbox service init success")

    @property
    def supports_running_delete(self) -> bool:
        return bool(self._operator and self._operator.supports_running_delete)

    def configure_operator(self, operator: AbstractOperator) -> None:
        """Install the deployment-selected operator before public routes start."""

        self._operator = operator

    def attach_snapshot_manager(self, manager: "SnapshotManager") -> None:
        """Bind the peer manager exactly once before routes become available."""

        if self._snapshot_manager is not None:
            raise RuntimeError("snapshot manager is already attached")
        self._snapshot_manager = manager

    @property
    def snapshot_runtime_configured(self) -> bool:
        return self._snapshot_manager is not None

    def _require_snapshot_manager(self) -> "SnapshotManager":
        if self._snapshot_manager is None:
            raise RuntimeError("snapshot manager is not attached")
        return self._snapshot_manager

    def _snapd_runtime(
        self, row: dict[str, Any], snapshot: LaunchableSnapshot | None = None
    ) -> SnapdSandboxRuntime | None:
        if not isinstance(self._operator, SnapdOperator) or not row.get("source_snapshot_id"):
            return None
        placement = _sandbox_placement(row)
        if _optional_host(row.get("host_ip")) and not placement:
            raise ValueError("persisted snapd placement is incomplete")
        return SnapdSandboxRuntime(
            sandbox_id=row["sandbox_id"],
            source_snapshot_id=row["source_snapshot_id"],
            sandbox_namespace=row.get("namespace", "default"),
            placement=SnapdPlacement(**placement) if placement else None,
            resource_spec=_resource_spec_from_row(row),
            snapshot_spec=snapshot.spec if snapshot is not None else None,
            source_worker_ip=snapshot.source_worker_ip if snapshot is not None else None,
        )

    async def extend_timeout(self, sandbox_id: str, timeout_seconds: int) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        current = await self._meta_store.get_timeout(sandbox_id) or {}
        expiry_key = env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY
        expiry = max(int(current.get(expiry_key, 0)), int(time.time()) + timeout_seconds)
        await self._meta_store.update_timeout(
            sandbox_id,
            {
                env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: str((timeout_seconds + 59) // 60),
                expiry_key: str(expiry),
            },
        )

    async def get_product_view(self, sandbox_id: str) -> SandboxProductView | None:
        row = await self._meta_store.get(sandbox_id, check_db=True)
        if row is None:
            return None
        token = row.get("auth_token")
        return SandboxProductView(
            sandbox_id=sandbox_id,
            sandbox_namespace=row.get("namespace", "default"),
            state=str(getattr(row.get("state"), "value", row.get("state", ""))).lower(),
            source_snapshot_id=(
                row.get("source_snapshot_id") if isinstance(row.get("source_snapshot_id"), str) else None
            ),
            image=str(row.get("image") or ""),
            auth_token=token if isinstance(token, str) and token else None,
        )

    async def list_active_snapshot_users(
        self,
        snapshot_id: str,
    ) -> tuple[ActiveSnapshotUser, ...]:
        users = []
        for row in await self._meta_store.list_snapshot_references(snapshot_id):
            state = str(getattr(row.get("state"), "value", row.get("state", ""))).lower()
            retained_checkpoint = state == State.STOPPED.value and row.get("pause_snapshot_id") == snapshot_id
            if state in _SNAPSHOT_REFERENCE_TERMINAL_STATES and not retained_checkpoint:
                continue
            users.append(ActiveSnapshotUser(
                sandbox_id=row["sandbox_id"],
                source_snapshot_id=snapshot_id,
                lifecycle_state=state,
                placement=_sandbox_placement(row),
            ))
        return tuple(users)

    async def require_snapshot_source(
        self,
        sandbox_id: str,
        *,
        require_running: bool = True,
    ) -> SnapshotSource:
        row = await self._meta_store.get_durable(sandbox_id)
        if row is None:
            raise BadRequestRockError(f"Sandbox {sandbox_id} not found")
        state = str(getattr(row.get("state"), "value", row.get("state", ""))).lower()
        if require_running and state != State.RUNNING.value:
            from rock.admin.service.snapshot_manager import SnapshotConflict

            raise SnapshotConflict(f"source sandbox {sandbox_id!r} is not RUNNING")
        placement = _sandbox_placement(row)
        node_id = placement.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            from rock.admin.service.snapshot_manager import SnapshotConflict

            raise SnapshotConflict(f"source sandbox {sandbox_id!r} has no durable placement")
        return SnapshotSource(
            sandbox_id=sandbox_id,
            sandbox_namespace=row.get("namespace", "default"),

            target=OperatorTarget(
                node_id=node_id,
                host_ip=placement.get("host_ip") if isinstance(placement.get("host_ip"), str) else None,
                host_name=placement.get("host_name") if isinstance(placement.get("host_name"), str) else None,
            ),
            resource_spec=_resource_spec_from_row(row),
        )

    async def launchable_snapshot(self, snapshot_id: str) -> LaunchableSnapshot | None:
        if self._snapshot_manager is None:
            return None
        return await self._snapshot_manager.get_launchable_snapshot(snapshot_id)

    async def start_from_snapshot(
        self,
        snapshot: LaunchableSnapshot,
        request: StartFromSnapshotRequest,
        *,
        sandbox_namespace: str,
        sandbox_image: str | None = None,
        deployment_config: DockerDeploymentConfig | None = None,
        sandbox_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        total_started = time.perf_counter()
        sandbox_id = request.sandbox_id or uuid.uuid4().hex
        resource_spec = dict(request.resource_spec)
        config = _snapshot_deployment_config(sandbox_id, snapshot.snapshot_id, resource_spec)
        if deployment_config is not None:
            if deployment_config.container_name != sandbox_id:
                raise BadRequestRockError("deployment config changed sandbox identity")
            config = deployment_config.model_copy(update={"source_snapshot_id": snapshot.snapshot_id})
        else:
            self.validate_sandbox_spec(self.rock_config.runtime, config)
            self._apply_user_auto_transition_policy(config)
        info = _snapshot_pending_info(
            sandbox_id,
            snapshot.snapshot_id,
            namespace=sandbox_namespace,
            resource_spec=resource_spec,
            sandbox_image=sandbox_image,
        )
        if sandbox_metadata is not None:
            # Product metadata is prepared by the normal start path. The
            # snapshot identity remains owned by this durable reservation.
            info = {**sandbox_metadata, **info}
        for field in ("auto_archive_seconds", "auto_delete_seconds"):
            info[field] = getattr(config, field)
        reservation_started = time.perf_counter()
        try:
            await self._meta_store.create(
                sandbox_id,
                info,
                timeout_info=SandboxTimeoutHelper.make_timeout_info(config.auto_clear_time),
                deployment_config=config,
            )
        except IntegrityError as error:
            if not is_sandbox_identity_conflict(error):
                raise
            from rock.admin.service.snapshot_manager import SnapshotConflict

            raise SnapshotConflict("sandbox identity already exists") from error
        self._log_snapshot_start_stage(
            snapshot.snapshot_id,
            sandbox_id,
            "durable_reservation",
            reservation_started,
            total_started,
        )
        operator_started = time.perf_counter()
        try:
            if isinstance(self._operator, SnapdOperator):
                operator_info = await self._operator.submit(config, runtime=self._snapd_runtime(info, snapshot))
            else:
                operator_info = await self._operator.submit(config)
        except OperatorLifecycleError as error:
            operator_info = error.sandbox_info
        result = operator_result_from_info(operator_info)
        self._log_snapshot_start_stage(
            snapshot.snapshot_id,
            sandbox_id,
            "operator_submit",
            operator_started,
            total_started,
        )
        if result.result_unknown:
            await self._persist_operator_observation(info, result)
            from rock.admin.service.snapshot_manager import SnapshotResultUnknown

            raise SnapshotResultUnknown(result.last_error or "sandbox start result is unknown")
        if result.code in {
            OperatorResultCode.FAILED,
            OperatorResultCode.IDENTITY_CONFLICT,
            OperatorResultCode.NOT_FOUND,
        }:
            await self._mark_launch_failed(sandbox_id, result.last_error or "snapshot sandbox start failed")
            from rock.admin.service.snapshot_manager import SnapshotConflict

            raise SnapshotConflict(result.last_error or "snapshot sandbox start failed")
        persistence_started = time.perf_counter()
        updated = await self._persist_operator_observation(info, result)
        if result.state == "RUNNING":
            await self._mark_alive(updated, result)
        current = await self._meta_store.get_durable(sandbox_id)
        assert current is not None
        self._log_snapshot_start_stage(
            snapshot.snapshot_id,
            sandbox_id,
            "persist_running",
            persistence_started,
            total_started,
        )
        return _start_result(current)

    @staticmethod
    def _log_snapshot_start_stage(
        snapshot_id: str,
        sandbox_id: str,
        phase: str,
        started: float,
        total_started: float,
    ) -> None:
        logger.info(
            json.dumps(
                {
                    "event": "admin_start_stage_completed",
                    "component": "sandbox_manager",
                    "snapshot_id": snapshot_id,
                    "sandbox_id": sandbox_id,
                    "phase": phase,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "total_duration_ms": round((time.perf_counter() - total_started) * 1000),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    async def snapshot_sandbox(
        self,
        source_sandbox_id: str,
        request: CreateSnapshotRequest,
    ) -> dict[str, Any]:
        source = await self.require_snapshot_source(source_sandbox_id)
        return await self._require_snapshot_manager().capture_snapshot(
            source,
            request,
        )

    async def fork_sandbox(
        self,
        source_sandbox_id: str,
        request: ForkSnapshotRequest,
        *,
        child_timeout_seconds: int | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        total_started = time.perf_counter()
        source_started = time.perf_counter()
        source = await self.require_snapshot_source(source_sandbox_id)
        self._log_snapshot_start_stage(
            request.snapshot_id or source_sandbox_id,
            f"fork:{request.count}",
            "fork_source_read",
            source_started,
            total_started,
        )
        snapshot_manager = self._require_snapshot_manager()
        resource_config = _snapshot_deployment_config(
            source_sandbox_id,
            request.snapshot_id or "pending-fork",
            {**source.resource_spec, **request.resource_spec},
        )
        self.validate_sandbox_spec(self.rock_config.runtime, resource_config)
        preparation_started = time.perf_counter()
        preparation = await snapshot_manager.prepare_fork(
            source,
            request,
            child_timeout_seconds=child_timeout_seconds,
        )
        self._log_snapshot_start_stage(
            preparation.snapshot_id,
            f"fork:{len(preparation.target_sandbox_ids)}",
            "fork_prepare",
            preparation_started,
            total_started,
        )
        reservation_started = time.perf_counter()
        accepted_ids: list[str] = []
        for sandbox_id in preparation.target_sandbox_ids:
            config = _snapshot_deployment_config(sandbox_id, preparation.snapshot_id, preparation.resource_spec)
            self._apply_user_auto_transition_policy(config)
            info = _snapshot_pending_info(
                sandbox_id,
                preparation.snapshot_id,
                namespace=preparation.sandbox_namespace,
                resource_spec=preparation.resource_spec,
            )
            for field in ("auto_archive_seconds", "auto_delete_seconds"):
                info[field] = getattr(config, field)
            timeout_info = SandboxTimeoutHelper.make_timeout_info(config.auto_clear_time)
            if child_timeout_seconds is not None:
                timeout_info = _timeout_info_seconds(child_timeout_seconds)
            try:
                await self._meta_store.create(
                    sandbox_id, info, timeout_info=timeout_info, deployment_config=config,
                )
            except IntegrityError as error:
                if not is_sandbox_identity_conflict(error):
                    raise
                continue
            accepted_ids.append(sandbox_id)
        # Only newly inserted child identities participate in the fork.
        preparation = replace(preparation, target_sandbox_ids=tuple(accepted_ids))
        self._log_snapshot_start_stage(
            preparation.snapshot_id,
            f"fork:{len(accepted_ids)}",
            "fork_child_db_reservation",
            reservation_started,
            total_started,
        )

        if not accepted_ids:
            record = await snapshot_manager.complete_fork(
                preparation,
                ForkExecutionResult(
                    code=OperatorResultCode.IDENTITY_CONFLICT,
                    state="FAILED",
                    children=tuple(),
                    last_error="all requested sandbox identities already exist",
                ),
            )
            return record, []

        plans = tuple(
            ForkChildLaunchPlan(sandbox_id, dict(preparation.resource_spec))
            for sandbox_id in preparation.target_sandbox_ids
        )
        operator_started = time.perf_counter()
        try:
            result = await snapshot_manager.execute_fork(preparation, plans)
        except Exception as error:
            from rock.admin.service.snapshot_manager import SnapshotConflict, SnapshotResultUnknown

            if isinstance(error, SnapshotResultUnknown):
                # The cut/launch may have committed.  Preserve CREATING and
                # PENDING rows for status reconciliation, not public replay.
                raise
            failed = ForkExecutionResult(
                code=OperatorResultCode.FAILED,
                state="FAILED",
                children=tuple(
                    ForkChildOutcome(
                        sandbox_id=child.sandbox_id,
                        state="FAILED",
                        last_error=str(error),
                    )
                    for child in plans
                ),
                last_error=str(error),
            )
            await self._apply_fork_outcomes(preparation, failed.children)
            await snapshot_manager.complete_fork(preparation, failed)
            if isinstance(error, SnapshotConflict):
                raise
            raise SnapshotConflict(str(error)) from error
        self._log_snapshot_start_stage(
            preparation.snapshot_id,
            f"fork:{len(plans)}",
            "fork_execute",
            operator_started,
            total_started,
        )
        outcomes = result.children
        persistence_started = time.perf_counter()
        children = await self._apply_fork_outcomes(preparation, outcomes)
        if result.code is OperatorResultCode.RESULT_UNKNOWN:
            from rock.admin.service.snapshot_manager import SnapshotResultUnknown

            raise SnapshotResultUnknown(result.last_error or "fork result is unknown")
        record = await snapshot_manager.complete_fork(preparation, result)
        self._log_snapshot_start_stage(
            preparation.snapshot_id,
            f"fork:{len(outcomes)}",
            "fork_persist_outcomes",
            persistence_started,
            total_started,
        )
        if result.code in {OperatorResultCode.FAILED, OperatorResultCode.IDENTITY_CONFLICT}:
            from rock.admin.service.snapshot_manager import SnapshotConflict

            raise SnapshotConflict(result.last_error or "snapd rejected the fork request")
        return record, children

    async def _apply_fork_outcomes(
        self,
        preparation,
        outcomes: tuple[ForkChildOutcome, ...],
    ) -> list[dict[str, Any]]:
        by_id = {outcome.sandbox_id: outcome for outcome in outcomes}
        results: list[dict[str, Any]] = []
        for sandbox_id in preparation.target_sandbox_ids:
            outcome = by_id.get(sandbox_id)
            if outcome is None:
                results.append({"sandbox_id": sandbox_id, "state": "PENDING"})
                continue
            row = await self._meta_store.get_durable(sandbox_id)
            assert row is not None
            if outcome.target.has_durable_placement:
                result = _outcome_operator_result(outcome)
                row = await self._persist_operator_observation(row, result)
            if outcome.state == "RUNNING":
                await self._mark_alive(row, _outcome_operator_result(outcome))
            elif outcome.state == "FAILED":
                await self._mark_launch_failed(sandbox_id, outcome.last_error or "fork child failed")
            current = await self._meta_store.get_durable(sandbox_id)
            assert current is not None
            results.append(_child_result(current, outcome))
        return results

    async def _persist_operator_observation(self, row: dict[str, Any], result) -> dict[str, Any]:
        updated = dict(row)
        placement = {
            key: value
            for key, value in {
                "node_id": result.target.node_id,
                "host_ip": result.target.host_ip,
                "host_name": result.target.host_name,
            }.items()
            if value is not None
        }
        existing_placement = _sandbox_placement(row)
        if existing_placement and placement and any(
            placement.get(key) is not None and placement[key] != value
            for key, value in existing_placement.items() if value is not None
        ):
            raise BadRequestRockError("operator changed durable sandbox placement")
        if placement:
            updated["host_ip"] = result.target.host_ip or updated.get("host_ip", "default")
            updated["host_name"] = result.target.host_name or result.target.node_id
            updated["snapd_node_id"] = result.target.node_id
        if row.get("source_snapshot_id"):
            _apply_guest_runtime_observation(updated, result)
        if result.metadata:
            updated["metadata"] = {**dict(updated.get("metadata") or {}), **dict(result.metadata)}
        await self._meta_store.update(row["sandbox_id"], updated)
        current = await self._meta_store.get_durable(row["sandbox_id"])
        assert current is not None
        return current

    async def _mark_alive(self, row: dict[str, Any], result) -> None:
        sm = await self._get_current_statemachine(row["sandbox_id"])
        if sm is None or sm.current_state.value != State.PENDING:
            return
        info = dict(row)
        info.update(result.metadata)
        info["state"] = State.RUNNING
        info["host_ip"] = result.target.host_ip or info.get("host_ip")
        info["host_name"] = result.target.host_name or result.target.node_id or info.get("host_name")
        await sm.send("alive", sandbox_id=row["sandbox_id"], meta_store=self._meta_store, sandbox_info=info)

    async def _mark_launch_failed(self, sandbox_id: str, reason: str) -> None:
        sm = await self._get_current_statemachine(sandbox_id)
        if sm is None or sm.current_state.value not in {State.PENDING, State.RUNNING}:
            return
        await sm.send("launch_failed", sandbox_id=sandbox_id, meta_store=self._meta_store, reason=reason)

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
        info = (
            await self._meta_store.get_durable(sandbox_id)
            if self.snapshot_runtime_configured
            else await self._meta_store.get(sandbox_id, check_db=True)
        )
        if info is None:
            return None
        return await SandboxStateMachine.from_state_value(info.get("state"), sandbox_info=info)

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
        sandbox_info["rock_authorization_encrypted"] = self._aes_encrypter.encrypt(rock_auth)
        sandbox_info["state"] = State.PENDING
        sandbox_info["create_time"] = get_iso8601_timestamp()

    @monitor_sandbox_operation()
    async def start_async(
        self,
        config: DeploymentConfig,
        user_info: UserInfo = {},
        cluster_info: ClusterInfo = {},
        *,
        use_template_resource_spec: bool = False,
        apply_image_mirror: bool = True,
    ) -> SandboxStartResponse:
        await self._check_sandbox_exists_in_redis(config)
        snapshot = None
        if self.snapshot_runtime_configured:
            if not isinstance(config, DockerDeploymentConfig):
                raise BadRequestRockError("SnapdOperator requires a Docker deployment config")
            snapshot = await self.launchable_snapshot(config.source_snapshot_id or config.image)
            if config.source_snapshot_id is not None and snapshot is None:
                from rock.admin.service.snapshot_manager import SnapshotNotFound

                raise SnapshotNotFound(f"snapshot {config.source_snapshot_id!r} does not exist")
            if snapshot is not None and snapshot.kind == "FULL":
                if "cpus" in config.model_fields_set and math.ceil(config.cpus) != snapshot.resource_spec["vcpus"]:
                    raise BadRequestRockError("FULL snapshot CPU count cannot be changed")
                if (
                    "memory" in config.model_fields_set
                    and parse_size_to_bytes(config.memory) != snapshot.resource_spec["memory_mib"] * 1024**2
                ):
                    raise BadRequestRockError("FULL snapshot memory size cannot be changed")
                config = config.model_copy(update={
                    "cpus": snapshot.resource_spec["vcpus"],
                    "memory": f"{snapshot.resource_spec['memory_mib']}M",
                    "disk": snapshot.resource_spec.get("disk"),
                })
                use_template_resource_spec = True

        if isinstance(config, DockerDeploymentConfig):
            await self.rock_config.update()
            await apply_start_config(
                self.rock_config,
                config,
                user_info.get("rock_authorization"),
                apply_image_mirror=apply_image_mirror and snapshot is None,
            )

        with StageTimer("startup_timing", f"[{config.image}] Init config", logger):
            if isinstance(config, DockerDeploymentConfig):
                docker_deployment_config: DockerDeploymentConfig = await self.deployment_manager.init_config(
                    config,
                    refresh_config=False,
                )
            else:
                docker_deployment_config = await self.deployment_manager.init_config(config)
        # Nacos is refreshed before normalization for Docker configs, or by
        # init_config for other config types. Validate only after that refresh.
        self.validate_sandbox_spec(self.rock_config.runtime, config)

        sandbox_id = docker_deployment_config.container_name
        self._apply_standard_spec(
            docker_deployment_config,
            use_template_resource_spec=use_template_resource_spec,
        )

        sandbox_info: SandboxInfo = {
            "sandbox_id": sandbox_id,
            "image": docker_deployment_config.image,
            "metadata": dict(docker_deployment_config.metadata),
            "env": dict(docker_deployment_config.env_vars),
            "cpus": docker_deployment_config.cpus,
            "memory": docker_deployment_config.memory,
            "num_gpus": docker_deployment_config.num_gpus,
            "accelerator_type": docker_deployment_config.accelerator_type,
        }
        if docker_deployment_config.disk is not None:
            sandbox_info["disk"] = docker_deployment_config.disk
        await self._build_sandbox_info_metadata(sandbox_info, user_info, cluster_info)
        if self.snapshot_runtime_configured:
            self._apply_user_auto_transition_policy(docker_deployment_config)
            snapshot_manager = self._require_snapshot_manager()
            if snapshot is None:
                snapshot = await snapshot_manager.prepare_base_image(
                    CreateBaseImageRequest(
                        oci_ref=docker_deployment_config.image,
                        platform=docker_deployment_config.platform or "linux/amd64",
                    ),
                    timeout_seconds=docker_deployment_config.startup_timeout or self._snapshot_template_start_timeout_seconds,
                )
            resource_spec = _resource_spec_from_row(sandbox_info)
            result = await snapshot_manager.start_async(
                snapshot.snapshot_id,
                StartFromSnapshotRequest(sandbox_id=sandbox_id, resource_spec=resource_spec),
                sandbox_namespace=user_info.get("namespace", "default"),
                sandbox_image=docker_deployment_config.image,
                deployment_config=docker_deployment_config,
                sandbox_metadata=sandbox_info,
            )
            return SandboxStartResponse(
                sandbox_id=result["sandbox_id"],
                source_snapshot_id=snapshot.snapshot_id,
                cpus=docker_deployment_config.cpus,
                memory=docker_deployment_config.memory,
                disk=docker_deployment_config.disk,
            )
        timeout_info = SandboxTimeoutHelper.make_timeout_info(docker_deployment_config.auto_clear_time)
        with StageTimer("startup_timing", f"[{sandbox_id}] Meta store create", logger):
            await self._meta_store.create(
                sandbox_id,
                sandbox_info,
                timeout_info=timeout_info,
                deployment_config=docker_deployment_config,
            )

        self._apply_user_auto_transition_policy(docker_deployment_config)
        if docker_deployment_config.auto_archive_seconds is not None:
            sandbox_info["auto_archive_seconds"] = docker_deployment_config.auto_archive_seconds
        if docker_deployment_config.auto_delete_seconds is not None:
            sandbox_info["auto_delete_seconds"] = docker_deployment_config.auto_delete_seconds
        with StageTimer("startup_timing", f"[{sandbox_id}] Meta store policy update", logger):
            await self._meta_store.update(sandbox_id, sandbox_info)

        try:
            with StageTimer("startup_timing", f"[{sandbox_id}] Operator submit", logger):
                submitted_info = await self._operator.submit(docker_deployment_config, user_info)
        except Exception as e:
            immediate_timeout_info = SandboxTimeoutHelper.make_timeout_info(0)
            sandbox_info["auto_archive_seconds"] = None
            sandbox_info["auto_delete_seconds"] = 0
            try:
                await self._meta_store.update(sandbox_id, sandbox_info)
                await self._meta_store.update_timeout(sandbox_id, immediate_timeout_info)
            except Exception:
                logger.exception("[%s] failed to update immediate cleanup metadata", sandbox_id)
            raise InternalServerRockError(
                f"Sandbox {sandbox_id} submission failed and has been marked for immediate deletion: {e}"
            ) from e
        create_time = sandbox_info.get("create_time")
        sandbox_info.update(submitted_info)
        await self._build_sandbox_info_metadata(sandbox_info, user_info, cluster_info)
        if create_time is not None:
            sandbox_info["create_time"] = create_time
        with StageTimer("startup_timing", f"[{sandbox_id}] Meta store submit update", logger):
            await self._meta_store.update(sandbox_id, sandbox_info)
        return SandboxStartResponse(
            sandbox_id=sandbox_id,
            host_name=sandbox_info.get("host_name"),
            host_ip=sandbox_info.get("host_ip"),
            disk=sandbox_info.get("disk"),
            disk_limit_rootfs=sandbox_info.get("disk"),
        )

    def _apply_standard_spec(
        self,
        config: DockerDeploymentConfig,
        *,
        use_template_resource_spec: bool = False,
    ) -> None:
        if self.rock_config.runtime.use_standard_spec_only and not use_template_resource_spec:
            logger.info(
                f"[{config.container_name}] Using standard spec only: "
                f"cpus={self.rock_config.runtime.standard_spec.cpus}, "
                f"memory={self.rock_config.runtime.standard_spec.memory}"
            )
            config.cpus = self.rock_config.runtime.standard_spec.cpus
            config.memory = self.rock_config.runtime.standard_spec.memory

    def _apply_user_auto_transition_policy(self, config: DockerDeploymentConfig) -> None:
        """Resolve the effective policy and enforce the cluster deadline cap."""
        cluster_delete_seconds = self.rock_config.lifecycle.auto_transition.auto_delete_seconds

        if config.auto_archive_seconds is not None:
            field = "auto_archive_seconds"
            seconds = config.auto_archive_seconds
            config.auto_delete_seconds = None
            config.remove_container = False
        elif config.auto_delete_seconds is not None:
            field = "auto_delete_seconds"
            seconds = config.auto_delete_seconds
        else:
            if cluster_delete_seconds is not None:
                config.auto_delete_seconds = cluster_delete_seconds
                config.remove_container = cluster_delete_seconds == 0
            return

        effective_seconds = seconds if cluster_delete_seconds is None else min(seconds, cluster_delete_seconds)
        setattr(config, field, effective_seconds)

        if field == "auto_delete_seconds":
            config.remove_container = effective_seconds == 0

    @monitor_sandbox_operation()
    async def restart_async(self, sandbox_id: str, *, timeout_seconds: int | None = None) -> SandboxStartResponse:
        sm = await self._get_current_statemachine(sandbox_id)
        if sm is None:
            raise BadRequestRockError(f"Sandbox {sandbox_id} not found")

        state = sm.current_state.value
        if self.snapshot_runtime_configured:
            await self._restart_from_checkpoint(sm, sandbox_id, 300 if timeout_seconds is None else timeout_seconds)
            info = await self._meta_store.get_durable(sandbox_id)
            return SandboxStartResponse(
                sandbox_id=sandbox_id, host_name=info.get("host_name"), host_ip=info.get("host_ip"),
            )
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
    async def start(
        self,
        config: DeploymentConfig,
        user_info: UserInfo = {},
        cluster_info: ClusterInfo = {},
    ) -> SandboxStartResponse:
        return await self._start_and_wait(config, user_info=user_info, cluster_info=cluster_info)

    @monitor_sandbox_operation()
    async def start_from_template(
        self,
        config: DeploymentConfig,
        user_info: UserInfo = {},
        cluster_info: ClusterInfo = {},
        *,
        wait_timeout: float | None = None,
    ) -> SandboxStartResponse:
        return await self._start_and_wait(
            config,
            user_info=user_info,
            cluster_info=cluster_info,
            use_template_resource_spec=True,
            apply_image_mirror=False,
            wait_timeout=wait_timeout,
        )

    async def _start_and_wait(
        self,
        config: DeploymentConfig,
        user_info: UserInfo,
        cluster_info: ClusterInfo,
        *,
        use_template_resource_spec: bool = False,
        wait_timeout: float | None = None,
        apply_image_mirror: bool = True,
    ) -> SandboxStartResponse:
        response = await self.start_async(
            config,
            user_info=user_info,
            cluster_info=cluster_info,
            use_template_resource_spec=use_template_resource_spec,
            apply_image_mirror=apply_image_mirror,
        )
        sandbox_id = response.sandbox_id
        wait_timeout = REQUEST_TIMEOUT_SECONDS if wait_timeout is None else wait_timeout

        async def poll_until_running():
            while True:
                await asyncio.sleep(1)
                status = await self.get_status(sandbox_id)
                if status.is_alive and status.host_ip:
                    return

        with StageTimer("startup_timing", f"[{sandbox_id}] Wait sandbox running", logger):
            try:
                await asyncio.wait_for(poll_until_running(), timeout=wait_timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"sandbox {sandbox_id} not running after {wait_timeout}s") from exc
        return response

    async def _stop_with_checkpoint(self, sm: SandboxStateMachine, sandbox_id: str, reason: StopReason) -> None:
        from rock.admin.service.snapshot_manager import SnapshotConflict, SnapshotNotFound, SnapshotResultUnknown

        if sm.current_state.value == State.RUNNING:
            info = dict(sm.sandbox_info)
            if not info.get("source_snapshot_id"):
                raise SnapshotConflict("Checkpoint stop requires a snapshot-backed sandbox")
            snapshots = self._require_snapshot_manager()
            snapshot_id = info.get("pause_snapshot_id")
            record = None
            if snapshot_id:
                try:
                    record = await snapshots.get_snapshot_record(snapshot_id)
                except SnapshotNotFound:
                    pass
                if record is not None and record["state"] == "FAILED":
                    snapshot_id = None
                    record = None
            if not snapshot_id:
                snapshot_id = f"pause-{uuid.uuid4().hex}"
                info["pause_snapshot_id"] = snapshot_id
                await self._meta_store.persist_lifecycle(sandbox_id, info)
            if record is None:
                source = await self.require_snapshot_source(sandbox_id)
                await snapshots.capture_snapshot(source, CreateSnapshotRequest(snapshot_id=snapshot_id))
            await snapshots.wait_remote_ready(snapshot_id)
            sm.sandbox_info = info
        try:
            await sm.send(
                "stop", sandbox_id=sandbox_id, operator=self._operator,
                meta_store=self._meta_store, reason=reason, auto_transition=None,
                snapd_runtime=self._snapd_runtime(sm.sandbox_info or {}),
            )
        except OperatorLifecycleError as error:
            if operator_result_from_info(error.sandbox_info).result_unknown:
                raise SnapshotResultUnknown(str(error)) from error
            raise SnapshotConflict(str(error)) from error
        except SnapdResultUnknown as error:
            raise SnapshotResultUnknown(str(error)) from error

    async def _restart_from_checkpoint(self, sm: SandboxStateMachine, sandbox_id: str, timeout_seconds: int) -> None:
        from rock.admin.service.snapshot_manager import SnapshotConflict, SnapshotResultUnknown

        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        state = sm.current_state.value
        info = dict(sm.sandbox_info)
        snapshot_id = info.get("pause_snapshot_id")
        if state == State.RUNNING:
            if snapshot_id:
                raise SnapshotConflict("Checkpoint stop is incomplete; retry stop or delete the sandbox")
            await self.extend_timeout(sandbox_id, timeout_seconds)
            return
        if state not in {State.STOPPED, State.PENDING} or not snapshot_id:
            raise SnapshotConflict("Restart requires a stopped sandbox with a retained checkpoint")
        try:
            if state == State.STOPPED:
                snapshot = await self._require_snapshot_manager().require_launchable_snapshot(snapshot_id)
                info["source_snapshot_id"] = snapshot_id
                info["spec"] = {**info["spec"], "source_snapshot_id": snapshot_id}
                await self._meta_store.persist_lifecycle(sandbox_id, info)
                sm.sandbox_info = info
                await sm.send(
                    "restart", sandbox_id=sandbox_id, operator=self._operator, meta_store=self._meta_store,
                    snapd_runtime=self._snapd_runtime(info, snapshot),
                    timeout_info={
                        env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: str((timeout_seconds + 59) // 60),
                        env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: str(int(time.time()) + timeout_seconds),
                    },
                )
            deadline = time.monotonic() + self._snapshot_template_start_timeout_seconds
            while True:
                status = await self.get_status(sandbox_id, include_all_states=True, refresh_timeout=False)
                if status.state == State.RUNNING and status.is_alive:
                    await self.extend_timeout(sandbox_id, timeout_seconds)
                    return
                if status.state in {State.STOPPED, State.DELETED}:
                    raise SnapshotConflict("Restart failed; checkpoint retained for retry")
                if time.monotonic() >= deadline:
                    raise SnapshotResultUnknown("Restart readiness timed out; checkpoint retained, retry to observe the launch")
                await asyncio.sleep(0.25)
        except OperatorLifecycleError as error:
            result = operator_result_from_info(error.sandbox_info)
            current = await self._meta_store.get_durable(sandbox_id)
            await self._persist_operator_observation(current or info, result)
            if result.result_unknown:
                raise SnapshotResultUnknown(str(error)) from error
            await self._mark_launch_failed(sandbox_id, str(error))
            raise SnapshotConflict(str(error)) from error
        except SnapdResultUnknown as error:
            raise SnapshotResultUnknown(str(error)) from error

    @monitor_sandbox_operation()
    async def stop(self, sandbox_id: str, reason: StopReason = StopReason.MANUAL) -> bool:
        sm = await self._get_current_statemachine(sandbox_id)
        if sm is None:
            logger.info(f"stop dangling sandbox {sandbox_id}")
            try:
                await self._operator.stop(sandbox_id, reason=reason)
            except ValueError as e:
                logger.error(f"ray get actor, actor {sandbox_id} not exist", exc_info=e)
        elif sm.current_state.value == State.STOPPED:
            await sm.send("stop_noop", sandbox_id=sandbox_id)
            return False
        else:
            checkpoint_stop = self.snapshot_runtime_configured and reason == StopReason.MANUAL
            if checkpoint_stop and (
                sm.current_state.value == State.RUNNING or (sm.sandbox_info or {}).get("pause_snapshot_id")
            ):
                await self._stop_with_checkpoint(sm, sandbox_id, reason)
                return True
            if (sm.sandbox_info or {}).get("pause_snapshot_id"):
                sm.sandbox_info = {**sm.sandbox_info, "pause_snapshot_id": None}
            try:
                await sm.send(
                    "stop",
                    sandbox_id=sandbox_id,
                    operator=self._operator,
                    meta_store=self._meta_store,
                    reason=reason,
                    auto_transition=self.rock_config.lifecycle.auto_transition,
                    snapd_runtime=self._snapd_runtime(sm.sandbox_info or {}),
                )
            except (OperatorLifecycleError, SnapdResultUnknown) as error:
                raise _snapshot_lifecycle_error(error) from error
            # `--rm` containers are already gone after stop; cascade to DELETED
            # so the metadata row doesn't linger in STOPPED.
            # Redis keys are gone after archive; re-read the effective policy
            # from DB status, falling back to the legacy spec for old records.
            sm = await self._get_current_statemachine(sandbox_id)
            stopped_info = (sm.sandbox_info or {}) if sm else {}
            archive_seconds = SandboxLifecycleHelper.resolve_auto_archive_seconds(stopped_info)
            delete_seconds = SandboxLifecycleHelper.resolve_auto_delete_seconds(stopped_info)
            spec = stopped_info.get("spec") or {}
            remove_container = archive_seconds is None and (
                delete_seconds == 0 or (delete_seconds is None and spec.get("remove_container"))
            )
            if remove_container:
                await sm.send(
                    "delete",
                    sandbox_id=sandbox_id,
                    operator=self._operator,
                    meta_store=self._meta_store,
                    reason=DeleteReason.IMMEDIATE,
                )
        return True

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
        deletable_states = {State.STOPPED, State.ARCHIVED}
        supports_running_delete = self._operator.supports_running_delete
        if supports_running_delete:
            deletable_states.add(State.RUNNING)
        if state not in deletable_states:
            raise BadRequestRockError(
                f"Sandbox {sandbox_id} cannot be deleted: current state is '{state.value}', must be stopped or archived first"
            )

        if supports_running_delete and state == State.RUNNING:
            # Active metadata is served from Redis, while the deployment spec
            # is stored only in the DB. Rebuild the generic deployment fields
            # required by operators that support deleting a live sandbox.
            sandbox_info = sm.sandbox_info or {}
            if not sandbox_info.get("spec"):
                sandbox_info["spec"] = {
                    "container_name": sandbox_id,
                    "image": sandbox_info.get("image") or DockerDeploymentConfig.model_fields["image"].default,
                    "memory": sandbox_info.get("memory") or DockerDeploymentConfig.model_fields["memory"].default,
                    "cpus": float(sandbox_info.get("cpus") or DockerDeploymentConfig.model_fields["cpus"].default),
                    "extended_params": sandbox_info.get("extended_params") or {},
                }
                sm.sandbox_info = sandbox_info

        delete_event = "delete_running" if supports_running_delete and state == State.RUNNING else "delete"
        try:
            await sm.send(
                delete_event,
                sandbox_id=sandbox_id,
                operator=self._operator,
                meta_store=self._meta_store,
                reason=reason,
                dir_storage=self._dir_storage,
                image_storage=self._image_storage,
                snapd_runtime=self._snapd_runtime(sm.sandbox_info or {}),
            )
        except (OperatorLifecycleError, SnapdResultUnknown) as error:
            raise _snapshot_lifecycle_error(error) from error

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

    async def _get_operator_status(self, sandbox_id: str, info: SandboxInfo) -> SandboxInfo | None:
        try:
            if isinstance(self._operator, SnapdOperator):
                return await self._operator.get_status(sandbox_id, runtime=self._snapd_runtime(info))
            return await self._operator.get_status(sandbox_id)
        except OperatorLifecycleError as error:
            if info.get("source_snapshot_id"):
                await self._persist_operator_observation(info, operator_result_from_info(error.sandbox_info))
                if not operator_result_from_info(error.sandbox_info).result_unknown:
                    await self._mark_launch_failed(sandbox_id, str(error))
            raise _snapshot_lifecycle_error(error) from error
        except SnapdResultUnknown as error:
            raise _snapshot_lifecycle_error(error) from error

    async def _try_advance_pending(self, sandbox_id: str, sm) -> dict | None:
        """Probe operator alive; fire ``alive`` transition if RUNNING. Returns operator info or None."""
        row = sm.sandbox_info or {}
        operator_sandbox_info = await self._get_operator_status(sandbox_id, row)
        if operator_sandbox_info is not None:
            operator_sandbox_info.setdefault("sandbox_id", sandbox_id)
        result = None if operator_sandbox_info is None else operator_result_from_info(operator_sandbox_info)
        if operator_sandbox_info is not None:
            operator_sandbox_info = {"sandbox_id": sandbox_id, **operator_sandbox_info}
        if result is not None and row.get("source_snapshot_id"):
            row = await self._persist_operator_observation(row, result)
        if operator_sandbox_info is None:
            return None
        operator_sandbox_info = {**row, **operator_sandbox_info}
        is_alive = operator_sandbox_info.get("state") == State.RUNNING
        if sm.current_state.value == State.PENDING and is_alive:
            await sm.send(
                "alive", sandbox_id=sandbox_id, meta_store=self._meta_store, sandbox_info=operator_sandbox_info
            )
        elif (
            sm.current_state.value == State.PENDING
            and self.snapshot_runtime_configured
            and row.get("source_snapshot_id")
            and result is not None
            and result.code in {OperatorResultCode.STOPPED, OperatorResultCode.FAILED}
        ):
            await sm.send(
                "runtime_stopped",
                sandbox_id=sandbox_id,
                meta_store=self._meta_store,
                reason=result.last_error or f"operator observed {result.state}",
            )
        return operator_sandbox_info

    @monitor_sandbox_operation()
    async def get_status(
        self,
        sandbox_id,
        include_all_states: bool = False,
        refresh_timeout: bool = True,
    ) -> SandboxStatusResponse:
        sandbox_info = (
            await self._meta_store.get_durable(sandbox_id)
            if self.snapshot_runtime_configured
            else await self._meta_store.get(sandbox_id, check_db=True)
        )
        if sandbox_info is None:
            raise BadRequestRockError(f"Sandbox {sandbox_id} not found")

        state = sandbox_info.get("state")
        is_alive = False
        status_persisted = False
        operator_sandbox_info = (
            None if sandbox_info.get("pause_snapshot_id") and state != State.PENDING
            else await self._get_operator_status(sandbox_id, sandbox_info)
        )
        if operator_sandbox_info is not None:
            operator_sandbox_info.setdefault("sandbox_id", sandbox_id)
        operator_result = None if operator_sandbox_info is None else operator_result_from_info(operator_sandbox_info)
        if operator_sandbox_info is not None:
            operator_sandbox_info = {"sandbox_id": sandbox_id, **operator_sandbox_info}
        if operator_result is not None and sandbox_info.get("source_snapshot_id"):
            sandbox_info = await self._persist_operator_observation(sandbox_info, operator_result)
            state = sandbox_info.get("state")
            if sandbox_info.get("pause_snapshot_id") and state == State.PENDING and operator_result.result_unknown:
                from rock.admin.service.snapshot_manager import SnapshotResultUnknown

                raise SnapshotResultUnknown(operator_result.last_error or "Checkpoint restart result is unknown")
        if operator_sandbox_info is not None:
            operator_sandbox_info = {**sandbox_info, **operator_sandbox_info}
            is_alive = operator_sandbox_info.get("state") == State.RUNNING

            if is_alive and state == State.PENDING:
                sm = await self._get_current_statemachine(sandbox_id)
                if sm.current_state.value == State.PENDING:
                    await sm.send(
                        "alive", sandbox_id=sandbox_id, meta_store=self._meta_store, sandbox_info=operator_sandbox_info
                    )
                    status_persisted = True

            observed_terminal = operator_result is not None and (
                operator_result.code in {OperatorResultCode.STOPPED, OperatorResultCode.FAILED}
                or operator_result.state in {"STOPPED", "FAILED", "DELETED"}
            )
            if (
                self.snapshot_runtime_configured
                and sandbox_info.get("source_snapshot_id")
                and state in {State.PENDING, State.RUNNING}
                and observed_terminal
            ):
                sm = await self._get_current_statemachine(sandbox_id)
                if sm is not None and sm.current_state.value in {State.PENDING, State.RUNNING}:
                    reason = operator_result.last_error or (
                        f"operator observed {operator_result.state}" if operator_result.state == "FAILED" else ""
                    )
                    await sm.send(
                        "runtime_stopped",
                        sandbox_id=sandbox_id,
                        meta_store=self._meta_store,
                        reason=reason,
                    )
                    sandbox_info = sm.sandbox_info or sandbox_info
                    state = sandbox_info.get("state")
                    status_persisted = True

            operator_host_ip = operator_sandbox_info.get("host_ip")
            if (
                not status_persisted
                and is_missing_host_ip(sandbox_info.get("host_ip"))
                and isinstance(operator_host_ip, str)
                and not is_missing_host_ip(operator_host_ip)
            ):
                await self._meta_store.update_host_ip_if_missing(sandbox_id, operator_host_ip)

            if refresh_timeout and operator_sandbox_info.get("state") in (State.PENDING, State.RUNNING):
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
            merged_sandbox_info = {**sandbox_info, **operator_sandbox_info}
            if not operator_sandbox_info.get("phases") and sandbox_info.get("phases"):
                merged_sandbox_info["phases"] = sandbox_info["phases"]
            sandbox_info = merged_sandbox_info
        timeout_info = await self._meta_store.get_timeout(sandbox_id)
        auto_stop_time, auto_archive_time, auto_delete_time = SandboxTimeoutHelper.auto_transition_times_for_status(
            sandbox_info.get("state"),
            sandbox_info,
            timeout_info,
        )

        disk = sandbox_info.get("disk")
        if (
            disk is None
            and sandbox_info.get("source_snapshot_id")
            and (
                sandbox_info.get("state") == State.RUNNING
                or (sandbox_info.get("state") == State.STOPPED and sandbox_info.get("pause_snapshot_id"))
            )
        ):
            # Native snapshot starts need not specify a disk quota. E2B info
            # still requires a size: use the immutable rootfs length, never an
            # invented default or the string "None".
            from rock.admin.service.snapshot_manager import SnapshotConflict

            try:
                snapshot = await self.launchable_snapshot(
                    sandbox_info.get("pause_snapshot_id") or sandbox_info["source_snapshot_id"]
                )
            except SnapshotConflict:
                # A local fork is usable before its snapshot is published.
                # Optional resource enrichment must not gate native status.
                snapshot = None
            if snapshot is not None:
                logical_bytes = snapshot.resource_spec.get("rootfs_bytes")
                if isinstance(logical_bytes, int) and logical_bytes > 0:
                    disk = f"{logical_bytes}B"

        return SandboxStatusResponse(
            sandbox_id=sandbox_id,
            source_snapshot_id=sandbox_info.get("source_snapshot_id"),
            # Legacy SDKs accept optional objects, but reject JSON null while
            # polling a snapd runtime that has no phases or assigned ports yet.
            status=sandbox_info.get("phases") or {},
            port_mapping=sandbox_info.get("port_mapping") or {},
            state=sandbox_info.get("state"),
            host_name=sandbox_info.get("host_name"),
            host_ip=sandbox_info.get("host_ip"),
            is_alive=is_alive,
            image=sandbox_info.get("image"),
            metadata=sandbox_info.get("metadata"),
            swe_rex_version=swe_version,
            gateway_version=gateway_version,
            user_id=sandbox_info.get("user_id"),
            experiment_id=sandbox_info.get("experiment_id"),
            namespace=sandbox_info.get("namespace"),
            cpus=sandbox_info.get("cpus"),
            memory=sandbox_info.get("memory"),
            num_gpus=sandbox_info.get("num_gpus"),
            accelerator_type=sandbox_info.get("accelerator_type"),
            disk=disk,
            disk_limit_rootfs=disk,
            start_time=sandbox_info.get("start_time"),
            stop_time=sandbox_info.get("stop_time"),
            create_time=sandbox_info.get("create_time"),
            archive_time=sandbox_info.get("archive_time"),
            delete_time=sandbox_info.get("delete_time"),
            auto_stop_time=auto_stop_time,
            auto_archive_time=auto_archive_time,
            auto_delete_time=auto_delete_time,
            state_history=sandbox_info.get("state_history", []),
            **_runtime_status_fields(operator_result),
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

    async def set_timeout(self, sandbox_id: str, timeout_seconds: int) -> None:
        try:
            status = await self.get_status(
                sandbox_id,
                include_all_states=True,
                refresh_timeout=False,
            )
        except BadRequestRockError:
            raise SandboxNotFoundRockError(f"Sandbox {sandbox_id} not found") from None
        if status.state != State.RUNNING:
            raise BadRequestRockError(f"Sandbox {sandbox_id} is not running")
        await self._meta_store.update_timeout(
            sandbox_id,
            SandboxTimeoutHelper.make_timeout_info(math.ceil(timeout_seconds / 60)),
        )

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
        """Run the primary-node lifecycle scans."""
        logger.info("[auto_transition] start")
        await self._auto_stop_expired()
        await self._auto_delete_stopped()
        await self._auto_archive_stopped()
        await self._auto_delete_archived()
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
        """Delete STOPPED sandboxes whose auto-delete policy is due."""
        now = datetime.datetime.now(zoneinfo.ZoneInfo(env_vars.ROCK_TIME_ZONE))

        try:
            candidates = await self._meta_store.list_expired_by(
                State.STOPPED.value,
                State.DELETED.value,
                now,
                limit=2000,
            )
        except Exception as e:
            logger.warning(f"[auto_delete] list_expired_by failed: {e}")
            return

        logger.info(f"[auto_delete] candidates={len(candidates)}, due_at={now.isoformat(timespec='seconds')}")
        deleted_count = await self._delete_expired_candidates(candidates, now, log_prefix="auto_delete")
        logger.info(f"[auto_delete] done: deleted={deleted_count}/{len(candidates)}")

    async def _delete_expired_candidates(
        self,
        candidates: list[SandboxInfo],
        now: datetime.datetime,
        *,
        log_prefix: str,
    ) -> int:
        """Delete due candidates concurrently without exceeding the Ray call budget."""
        semaphore = asyncio.Semaphore(_AUTO_DELETE_CONCURRENCY)

        async def delete_one(info: SandboxInfo) -> bool:
            sandbox_id = info.get("sandbox_id", "")
            if not sandbox_id:
                return False

            due_time = SandboxLifecycleHelper.parse_iso8601_timestamp(info.get("auto_transition_time"))
            if due_time is None:
                return False

            if now < due_time:
                logger.info(f"[{log_prefix}] {sandbox_id} (state={info.get('state')}) not due until {due_time}")
                return False

            async with semaphore:
                try:
                    logger.info(f"[{log_prefix}] {sandbox_id} (state={info.get('state')}) due at {due_time}, deleting")
                    await self.delete(sandbox_id, reason=DeleteReason.EXPIRED)
                    return True
                except Exception as e:
                    logger.error(f"[{log_prefix}] {sandbox_id}: {e}", exc_info=True)
                    return False

        results = await asyncio.gather(*(delete_one(info) for info in candidates))
        return sum(results)

    async def _auto_delete_archived(self) -> None:
        """Delete ARCHIVED sandboxes at the cluster archive-retention deadline."""
        now = datetime.datetime.now(zoneinfo.ZoneInfo(env_vars.ROCK_TIME_ZONE))

        try:
            candidates = await self._meta_store.list_expired_by(
                State.ARCHIVED.value,
                State.DELETED.value,
                now,
                limit=1000,
            )
        except Exception as e:
            logger.warning(f"[auto_delete_archived] list_expired_by failed: {e}")
            return

        logger.info(f"[auto_delete_archived] candidates={len(candidates)}, due_at={now.isoformat(timespec='seconds')}")
        deleted_count = await self._delete_expired_candidates(candidates, now, log_prefix="auto_delete_archived")
        logger.info(f"[auto_delete_archived] done: deleted={deleted_count}/{len(candidates)}")

    async def _auto_archive_stopped(self) -> None:
        """Archive STOPPED sandboxes whose auto-archive policy is due."""
        if not self._operator or not self._dir_storage or not self._image_storage:
            logger.info("[auto_archive] skipped (storage not configured)")
            return

        now = datetime.datetime.now(zoneinfo.ZoneInfo(env_vars.ROCK_TIME_ZONE))

        try:
            stopped_list = await self._meta_store.list_expired_by(
                State.STOPPED.value,
                State.ARCHIVED.value,
                now,
                limit=1000,
            )
        except Exception as e:
            logger.warning(f"[auto_archive] list_expired_by failed: {e}")
            return

        logger.info(f"[auto_archive] candidates={len(stopped_list)}, due_at={now.isoformat(timespec='seconds')}")
        archived_count = 0
        for info in stopped_list:
            sandbox_id = info.get("sandbox_id", "")
            if not sandbox_id:
                continue

            due_time = SandboxLifecycleHelper.parse_iso8601_timestamp(info.get("auto_transition_time"))
            if due_time is None:
                continue

            if now < due_time:
                logger.info(f"[auto_archive] {sandbox_id} not due until {due_time}")
                continue
            try:
                logger.info(f"[auto_archive] {sandbox_id} due at {due_time}, archiving")
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

        if (sm.sandbox_info or {}).get("pause_snapshot_id"):
            raise BadRequestRockError("archive is not supported for a sandbox with a retained checkpoint")

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
                await sm.send(
                    "archive_done",
                    sandbox_id=sandbox_id,
                    meta_store=self._meta_store,
                    auto_transition=self.rock_config.lifecycle.auto_transition,
                )
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
                    auto_transition=self.rock_config.lifecycle.auto_transition,
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

            started_at = SandboxLifecycleHelper.get_current_state_started_at(info.get("state_history", []), "archiving")
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
                    auto_transition=self.rock_config.lifecycle.auto_transition,
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
                started_at = SandboxLifecycleHelper.get_current_state_started_at(
                    info.get("state_history", []), "pending"
                )
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
                                auto_transition=self.rock_config.lifecycle.auto_transition,
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


def _snapshot_lifecycle_error(error: OperatorLifecycleError | SnapdResultUnknown) -> Exception:
    from rock.admin.service.snapshot_manager import SnapshotConflict, SnapshotResultUnknown

    if isinstance(error, SnapdResultUnknown) or operator_result_from_info(error.sandbox_info).result_unknown:
        return SnapshotResultUnknown(str(error))
    return SnapshotConflict(str(error))


def _snapshot_deployment_config(
    sandbox_id: str,
    snapshot_id: str,
    resource_spec: dict[str, Any],
) -> DockerDeploymentConfig:
    memory_mib = max(1, int(resource_spec.get("memory_mib", 512)))
    return DockerDeploymentConfig(
        image=f"snapshot:{snapshot_id}",
        source_snapshot_id=snapshot_id,
        container_name=sandbox_id,
        cpus=float(resource_spec.get("vcpus", resource_spec.get("cpus", 1))),
        memory=f"{memory_mib}M",
        disk=resource_spec.get("disk"),
    )


def _snapshot_pending_info(
    sandbox_id: str,
    snapshot_id: str,
    *,
    namespace: str,
    resource_spec: dict[str, Any],
    sandbox_image: str | None = None,
) -> SandboxInfo:
    now = get_iso8601_timestamp()
    return {
        "sandbox_id": sandbox_id,
        "source_snapshot_id": snapshot_id,
        "state": State.PENDING,
        "image": sandbox_image or f"snapshot:{snapshot_id}",
        "host_ip": "default",
        "auth_token": secrets.token_urlsafe(32),
        "namespace": namespace,
        "create_time": now,
        "cpus": float(resource_spec.get("vcpus", resource_spec.get("cpus", 1))),
        "memory": f"{max(1, int(resource_spec.get('memory_mib', 512)))}M",
        "disk": resource_spec.get("disk"),
    }


def _sandbox_placement(row: dict[str, Any]) -> dict[str, Any]:
    host_ip = _optional_host(row.get("host_ip"))
    node_id = row.get("snapd_node_id") or (row.get("metadata") or {}).get("snapd_node_id") or row.get("host_name")
    if host_ip is None or not node_id:
        return {}
    return {"node_id": node_id, "host_ip": host_ip, "host_name": row.get("host_name")}


def _resource_spec_from_row(row: dict[str, Any]) -> dict[str, Any]:
    spec = row.get("spec") or {}
    if "memory_mib" in spec or "vcpus" in spec:
        return dict(spec)
    memory = row.get("memory") or spec.get("memory") or "512M"
    return {
        "vcpus": max(1, math.ceil(float(row.get("cpus") or spec.get("cpus") or 1))),
        "memory_mib": max(1, parse_size_to_bytes(memory) // (1024 * 1024)),
        **({"disk": row.get("disk") or spec.get("disk")} if row.get("disk") or spec.get("disk") else {}),
    }


def _start_result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandbox_id": row["sandbox_id"],
        "source_snapshot_id": row["source_snapshot_id"],
        "state": str(getattr(row.get("state"), "value", row.get("state", State.PENDING.value))).upper(),
    }


def _child_result(row: dict[str, Any], outcome: ForkChildOutcome | None = None) -> dict[str, Any]:
    state = str(getattr(row.get("state"), "value", row.get("state", State.PENDING.value))).upper()
    result = {
        "sandbox_id": row["sandbox_id"],
        "state": "FAILED" if state == "STOPPED" and row.get("last_error") else state,
        "placement": _sandbox_placement(row),
    }
    error = outcome.last_error if outcome is not None else row.get("last_error")
    if isinstance(error, str) and error:
        result["last_error"] = error
    result.update(_runtime_status_fields(outcome))
    return result


def _outcome_operator_result(outcome: ForkChildOutcome) -> OperatorSandboxResult:
    code = (
        OperatorResultCode.RUNNING
        if outcome.state == "RUNNING"
        else OperatorResultCode.RESULT_UNKNOWN
        if outcome.state in {"PENDING", "STARTING"}
        else OperatorResultCode.FAILED
    )
    return OperatorSandboxResult(
        sandbox_id=outcome.sandbox_id,
        code=code,
        state=outcome.state
        if outcome.state in {"PENDING", "STARTING", "RUNNING", "STOPPED", "FAILED", "DELETED"}
        else "FAILED",  # type: ignore[arg-type]
        target=outcome.target,
        last_error=outcome.last_error,
        guest_endpoint=outcome.guest_endpoint,
        vmm_pid=outcome.vmm_pid,
        rocklet_artifact=outcome.rocklet_artifact,
    )


def _apply_guest_runtime_observation(
    updated: dict[str, Any],
    result: OperatorSandboxResult,
) -> None:
    """Persist or release the endpoint owned by a snapshot-backed runtime."""

    observation = (result.guest_endpoint, result.vmm_pid, result.rocklet_artifact)
    complete = all(value is not None for value in observation)
    if any(value is not None for value in observation) and not complete:
        raise InternalServerRockError("snapd returned an incomplete guest runtime observation")
    if result.state == "RUNNING":
        if not complete:
            raise InternalServerRockError("snapd reported RUNNING before guest Rocklet readiness")
        try:
            endpoint_model = GuestEndpoint.model_validate(result.guest_endpoint)
            RockletArtifactIdentity.model_validate(result.rocklet_artifact)
        except ValidationError as error:
            raise InternalServerRockError("snapd returned an invalid guest runtime observation") from error
        if (
            endpoint_model.guest_port != Port.PROXY.value
            or endpoint_model.host_ip != result.target.host_ip
            or type(result.vmm_pid) is not int
            or not 1 <= result.vmm_pid <= 4_294_967_295
        ):
            raise InternalServerRockError("snapd returned an invalid guest runtime observation")
        endpoint = endpoint_model.model_dump(mode="json")
        port_mapping = dict(updated.get("port_mapping") or {})
        port_mapping[Port.PROXY.value] = endpoint["host_port"]
        updated["port_mapping"] = port_mapping
        return
    if complete:
        raise InternalServerRockError("snapd exposed a routable guest endpoint for a non-running runtime")
    if result.state in {"STOPPED", "FAILED", "DELETED"}:
        port_mapping = dict(updated.get("port_mapping") or {})
        port_mapping.pop(Port.PROXY.value, None)
        port_mapping.pop(str(Port.PROXY.value), None)
        updated["port_mapping"] = port_mapping


def _runtime_status_fields(observation: OperatorSandboxResult | ForkChildOutcome | None) -> dict[str, Any]:
    """Debug details are live observations, never a second persisted status."""
    return {
        field: value
        for field in ("guest_endpoint", "vmm_pid", "rocklet_artifact")
        if (value := getattr(observation, field, None)) is not None
    }


def _optional_host(value: object) -> str | None:
    return value if isinstance(value, str) and value not in {"", "default", "unknown"} else None


def _timeout_info_seconds(seconds: int) -> dict[str, str]:
    from rock import env_vars

    return {
        env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: str(max(1, math.ceil(seconds / 60))),
        env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: str(int(time.time()) + seconds),
    }
