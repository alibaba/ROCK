"""Admin-side snapd requests with Ray-backed placement and resource reservations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

import ray
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

from rock.admin.snapd.client import SnapdAPIError, SnapdResultUnknown
from rock.admin.snapd.protocol import (
    MAX_FORK_CHILDREN,
    DeleteSandboxRequest,
    GuestEndpoint,
    SandboxStatusResult,
    StartSandboxFromSnapshotRequest,
    StopSandboxRequest,
)
from rock.common.constants import StopReason
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.snapshot import AbstractSnapshotOperator
from rock.utils.format import parse_size_to_bytes

if TYPE_CHECKING:
    from rock.actions.sandbox.sandbox_info import SandboxInfo
    from rock.admin.core.ray_service import RayService
    from rock.admin.snapd.worker_client import _SnapdClientPool
    from rock.deployments.config import DeploymentConfig
    from rock.sandbox.snapshot_types import SnapshotSource

DEFAULT_SNAPD_REQUEST_TIMEOUT_MS = 30_000


def advertise_guest_endpoint(endpoint: Any, worker_ip: str) -> dict[str, Any]:
    """Combine Ray's authoritative worker address with snapd's proxy port."""
    payload = endpoint.model_dump(mode="json") if hasattr(endpoint, "model_dump") else dict(endpoint)
    return GuestEndpoint(host_ip=worker_ip, **payload).model_dump(mode="json")


def _snapd_resource_spec(resources: dict[str, Any]) -> dict[str, Any]:
    result = dict(resources)
    result["cpu"] = math.ceil(float(resources.get("vcpus", resources.get("cpus", 1))))
    result.pop("vcpus", None)
    result.pop("cpus", None)
    return result


class OperatorResultCode(str, Enum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    NOT_FOUND = "NOT_FOUND"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class OperatorTarget:
    node_id: str | None = None
    host_ip: str | None = None
    host_name: str | None = None

    @property
    def has_durable_placement(self) -> bool:
        return any((self.node_id, self.host_ip, self.host_name))


@dataclass(frozen=True, slots=True)
class OperatorSandboxResult:
    sandbox_id: str
    code: OperatorResultCode
    state: Literal["PENDING", "STARTING", "RUNNING", "STOPPED", "FAILED", "DELETED"]

    target: OperatorTarget = field(default_factory=OperatorTarget)
    last_error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    guest_endpoint: dict[str, Any] | None = None
    vmm_pid: int | None = None
    rocklet_artifact: dict[str, Any] | None = None

    @property
    def result_unknown(self) -> bool:
        return self.code is OperatorResultCode.RESULT_UNKNOWN


class OperatorLifecycleError(RuntimeError):
    """A failed/unknown lifecycle call retaining its standard observation.

    Managers must persist an unknown placement before reporting a lost reply;
    otherwise a retry or cleanup could lose track of a running VM.
    """

    def __init__(self, info: dict[str, Any]) -> None:
        self.sandbox_info = info
        observation = getattr(info, "operator_observation", None)
        super().__init__((observation.last_error if observation else None) or "operator lifecycle failed")

    def __reduce__(self):
        # Retain the observation when serializing this exception.
        return type(self), (self.sandbox_info,)


class _OperatorSandboxInfo(dict):
    """Carry a live RPC observation without adding fields to persisted SandboxInfo."""

    def __init__(self, info: dict[str, Any], observation: OperatorSandboxResult) -> None:
        super().__init__(info)
        self.operator_observation = observation


def sandbox_info_from_result(result: OperatorSandboxResult) -> dict[str, Any]:
    from rock.actions.sandbox.response import State

    info = {
        "sandbox_id": result.sandbox_id,
        "state": State.RUNNING
        if result.state == "RUNNING"
        else (State.STOPPED if result.state in {"STOPPED", "FAILED", "DELETED"} else State.PENDING),
        "metadata": dict(result.metadata),
    }
    if result.target.host_ip is not None:
        info["host_ip"] = result.target.host_ip
    if result.target.host_name is not None:
        info["host_name"] = result.target.host_name
    if result.target.node_id is not None:
        info["snapd_node_id"] = result.target.node_id
    if result.guest_endpoint is not None:
        info["port_mapping"] = {result.guest_endpoint["guest_port"]: result.guest_endpoint["host_port"]}
    return _OperatorSandboxInfo(info, result)


def operator_result_from_info(info: dict[str, Any]) -> OperatorSandboxResult:
    """Observe a normal SandboxInfo without changing the public Operator API."""
    observation = getattr(info, "operator_observation", None)
    if observation is not None:
        return observation
    state = str(getattr(info.get("state"), "value", info.get("state", "PENDING"))).upper()
    if state not in {"PENDING", "STARTING", "RUNNING", "STOPPED", "FAILED", "DELETED"}:
        state = "PENDING"
    code = {
        "RUNNING": OperatorResultCode.RUNNING,
        "STOPPED": OperatorResultCode.STOPPED,
        "FAILED": OperatorResultCode.FAILED,
        "DELETED": OperatorResultCode.STOPPED,
    }.get(state, OperatorResultCode.ACCEPTED)
    return OperatorSandboxResult(
        sandbox_id=info["sandbox_id"],
        code=code,
        state=state,
        target=OperatorTarget(
            node_id=info.get("snapd_node_id"),
            host_ip=info.get("host_ip"),
            host_name=info.get("host_name"),
        ),
        last_error=info.get("last_error"),
        metadata=dict(info.get("metadata") or {}),
    )


@dataclass(frozen=True, slots=True)
class SnapshotOperationResult:
    snapshot_id: str
    code: OperatorResultCode
    state: Literal["CREATING", "LOCAL_READY", "REMOTE_READY", "FAILED", "DELETED"]
    payload: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class ForkOperationResult(SnapshotOperationResult):
    children: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class SnapdPlacement:
    node_id: str
    host_ip: str | None = None
    host_name: str | None = None


@dataclass(frozen=True, slots=True)
class SnapdSandboxRuntime:
    """Execution data resolved by the Manager; never a persistence handle."""

    sandbox_id: str
    source_snapshot_id: str
    sandbox_namespace: str
    placement: SnapdPlacement | None
    resource_spec: dict[str, Any] = field(default_factory=dict)
    snapshot_spec: dict[str, Any] | None = None
    source_worker_ip: str | None = None


@dataclass
class _SandboxActor:
    sandbox_id: str
    handle: Any
    generation: str
    placement: SnapdPlacement | None
    ready: bool = True


class SnapdOperator(AbstractOperator, AbstractSnapshotOperator):
    """Reserve Ray resources, then call worker snapd directly from Admin."""

    supports_running_delete = True

    def __init__(
        self,
        ray_service: RayService,
        *,
        _client_pool: _SnapdClientPool | None = None,
        request_timeout_ms: int = DEFAULT_SNAPD_REQUEST_TIMEOUT_MS,
    ) -> None:
        from rock.admin.snapd.worker_client import _SnapdClientPool

        if request_timeout_ms <= 0:
            raise ValueError("snapd request timeouts must be positive")
        self._ray_service = ray_service
        self._snapd_clients = _client_pool or _SnapdClientPool()
        self._request_timeout_ms = request_timeout_ms
        self._disk_scheduling_enabled = ray.is_initialized() and "disk" in ray.cluster_resources()

    async def submit(
        self, config: DeploymentConfig, user_info: dict = {}, *, runtime: SnapdSandboxRuntime | None = None
    ) -> SandboxInfo:
        del user_info
        return await self._start(config, runtime=runtime)

    async def restart(
        self, config: DeploymentConfig, host_ip: str | None = None, *, runtime: SnapdSandboxRuntime | None = None
    ) -> SandboxInfo:
        return await self._start(config, runtime=runtime, host_ip=host_ip, restarting=True)

    async def _start(
        self,
        config: DeploymentConfig,
        *,
        runtime: SnapdSandboxRuntime | None,
        host_ip: str | None = None,
        restarting: bool = False,
    ) -> SandboxInfo:
        from rock.deployments.config import DockerDeploymentConfig

        if not isinstance(config, DockerDeploymentConfig) or not config.container_name:
            raise ValueError("SnapdOperator requires DockerDeploymentConfig with container_name")
        if runtime is None or runtime.snapshot_spec is None:
            raise ValueError("SnapdOperator requires a Manager-resolved snapshot runtime")
        if runtime.sandbox_id != config.container_name:
            raise ValueError("runtime sandbox identity differs from deployment config")
        if config.source_snapshot_id not in {None, runtime.source_snapshot_id}:
            raise SnapdResultUnknown("config source snapshot differs from durable sandbox identity")
        placement = runtime.placement
        if restarting and placement is None:
            raise SnapdResultUnknown("restart requires durable snapd placement")
        if host_ip and (placement is None or host_ip != placement.host_ip):
            raise SnapdResultUnknown("requested host differs from durable snapd placement")
        previous = await self._find_actor(runtime.sandbox_id)
        if previous is not None:
            try:
                info = await self._runtime_status(runtime.sandbox_id, previous.placement)
            except OperatorLifecycleError:
                raise
            except Exception as error:
                raise self._lifecycle_error(previous.sandbox_id, previous.placement, str(error)) from error
            if info is not None and info.state in {"STOPPED", "FAILED"}:
                await self._kill_actor(previous)
        created: list[_SandboxActor] = []
        try:
            actor = await self._create_actor(
                runtime.sandbox_id,
                runtime.sandbox_namespace,
                runtime.resource_spec,
                placement,
                created=created,
            )
        except BaseException as error:
            cleanup = asyncio.create_task(self._abort_unstarted(created))
            try:
                await asyncio.shield(cleanup)
            except Exception as cleanup_error:
                if not isinstance(error, asyncio.CancelledError):
                    error = SnapdResultUnknown(f"{error}; actor cleanup is unknown: {cleanup_error}")
            if isinstance(error, asyncio.CancelledError):
                raise
            # A newly scheduled actor supplies its own placement; a pending
            # actor may not have a node yet. Never invent a target for it.
            failed_placement = created[0].placement if created else placement
            raise OperatorLifecycleError(
                sandbox_info_from_result(
                    OperatorSandboxResult(
                        runtime.sandbox_id,
                        OperatorResultCode.RESULT_UNKNOWN,
                        "PENDING",
                        target=(
                            OperatorTarget(
                                failed_placement.node_id, failed_placement.host_ip, failed_placement.host_name
                            )
                            if failed_placement is not None
                            else OperatorTarget()
                        ),
                        last_error=str(error),
                    )
                )
            ) from error
        try:
            info = await self._start_sandbox(actor, runtime)
        except OperatorLifecycleError as error:
            if not operator_result_from_info(error.sandbox_info).result_unknown:
                try:
                    await self._kill_actor(actor)
                except Exception as cleanup_error:
                    raise self._lifecycle_error(
                        actor.sandbox_id, actor.placement, f"{error}; actor cleanup is unknown: {cleanup_error}"
                    ) from cleanup_error
            raise
        except Exception as error:
            raise self._lifecycle_error(actor.sandbox_id, actor.placement, str(error)) from error
        result = operator_result_from_info(info)
        if result.state in {"STOPPED", "FAILED", "DELETED"}:
            await self._kill_actor(actor)
        return info

    async def get_status(self, sandbox_id: str, *, runtime: SnapdSandboxRuntime | None = None) -> SandboxInfo | None:
        actor, placement = await self._sandbox_target(sandbox_id, runtime)
        if placement is None:
            return None
        try:
            result = await self._runtime_status(sandbox_id, placement)
            if result is None:
                return None
            if actor is None and result.state in {"STARTING", "RUNNING"}:
                raise self._lifecycle_error(
                    sandbox_id, placement, "VM is still present but its resource-owning actor is gone; stop or delete it"
                )
            if actor is not None and result.state in {"STOPPED", "FAILED"}:
                await self._kill_actor(actor)
            return self._status_info(sandbox_id, placement, result)
        except OperatorLifecycleError:
            raise
        except Exception as error:
            raise self._lifecycle_error(sandbox_id, placement, str(error)) from error

    async def stop(
        self, sandbox_id: str, reason: StopReason = StopReason.MANUAL, *, runtime: SnapdSandboxRuntime | None = None
    ) -> bool:
        actor, placement = await self._sandbox_target(sandbox_id, runtime)
        if placement is None:
            return True
        try:
            await self._stop_sandbox(sandbox_id, placement, reason)
            if actor is not None:
                await self._kill_actor(actor)
            return True
        except OperatorLifecycleError:
            raise
        except Exception as error:
            raise self._lifecycle_error(sandbox_id, placement, str(error)) from error

    async def delete(
        self, config: DeploymentConfig, host_ip: str | None = None, *, runtime: SnapdSandboxRuntime | None = None
    ) -> bool:
        sandbox_id = config.container_name
        if not sandbox_id:
            raise ValueError("delete requires sandbox_id")
        actor, placement = await self._sandbox_target(sandbox_id, runtime)
        if placement is None:
            return True
        try:
            if host_ip and host_ip != placement.host_ip:
                raise SnapdResultUnknown("requested host differs from durable snapd placement")
            await self._snapd_clients.client(placement.host_ip).delete_sandbox(
                sandbox_id, DeleteSandboxRequest(), deadline_unix_ms=self._deadline()
            )
            if actor is not None:
                await self._kill_actor(actor)
            return True
        except OperatorLifecycleError:
            raise
        except Exception as error:
            raise self._lifecycle_error(sandbox_id, placement, str(error)) from error

    @staticmethod
    def _actor_name(sandbox_id: str) -> str:
        digest = hashlib.sha256(sandbox_id.encode("utf-8")).hexdigest()[:32]
        return f"snapd-sandbox-{digest}"

    async def _get(self, ref: Any) -> Any:
        try:
            return await self._ray_service.async_ray_get(ref, timeout=self._request_timeout_ms / 1000 + 5)
        except Exception as error:
            raise SnapdResultUnknown(f"Ray actor reply is unknown: {error}") from error

    async def _find_actor(self, sandbox_id: str) -> _SandboxActor | None:
        actor = await self._ray_service.async_try_get_actor(self._actor_name(sandbox_id))
        if actor is None:
            return None
        return await self._actor_target(sandbox_id, actor)

    async def _actor_target(self, sandbox_id: str, handle: Any) -> _SandboxActor:
        identity, placement = await asyncio.gather(
            self._get(handle.identity.remote()), self._get(handle.placement.remote())
        )
        if identity.get("sandbox_id") != sandbox_id or not identity.get("actor_generation"):
            raise SnapdResultUnknown("Ray actor identity does not match sandbox")
        return _SandboxActor(
            sandbox_id,
            handle,
            identity["actor_generation"],
            SnapdPlacement(placement["ray_node_id"], placement["host_ip"], placement["ray_node_id"]),
        )

    def _actor_options(
        self, sandbox_id: str, resources: dict[str, Any], placement: SnapdPlacement | None = None
    ) -> dict:
        cpus = math.ceil(float(resources.get("vcpus", resources.get("cpus", 1))))
        memory = (
            int(resources["memory_mib"]) * 1024**2
            if "memory_mib" in resources
            else parse_size_to_bytes(resources.get("memory", "512M"))
        )
        if cpus <= 0 or memory <= 0:
            raise ValueError("snapd actor CPU and memory must be positive")
        # New sandboxes use Ray scheduling. Forks and existing runtimes must
        # stay on the worker that owns their node-local runtime state.
        custom = {}
        if resources.get("disk") is not None and self._disk_scheduling_enabled:
            custom["disk"] = float(parse_size_to_bytes(resources["disk"]))
        options = {
            "name": self._actor_name(sandbox_id),
            "lifetime": "detached",
            "num_cpus": cpus,
            "memory": memory,
            "resources": custom,
        }
        if placement is not None:
            options["scheduling_strategy"] = NodeAffinitySchedulingStrategy(node_id=placement.node_id, soft=False)
        return options

    async def _create_actor(
        self,
        sandbox_id: str,
        sandbox_namespace: str,
        resources: dict[str, Any],
        placement: SnapdPlacement | None = None,
        *,
        created: list[_SandboxActor] | None = None,
    ) -> _SandboxActor:
        from rock.sandbox.operator.snapd_actor import SnapdSandboxActor

        generation = uuid.uuid4().hex
        owned = None
        handle = await self._ray_service.async_try_get_actor(self._actor_name(sandbox_id))
        if handle is None:
            async with self._ray_service.get_ray_rwlock().read_lock():
                try:
                    handle = SnapdSandboxActor.options(**self._actor_options(sandbox_id, resources, placement)).remote(
                        sandbox_id,
                        sandbox_namespace,
                        dict(resources),
                        actor_generation=generation,
                    )
                except ValueError:
                    # Another request may have created the same named actor.
                    handle = await self._ray_service.async_try_get_actor(self._actor_name(sandbox_id))
                    if handle is None:
                        raise
                else:
                    if created is not None:
                        # Register the owned handle before placement/identity can time out.
                        owned = _SandboxActor(sandbox_id, handle, generation, placement, ready=False)
                        created.append(owned)
        actor = await self._actor_target(sandbox_id, handle)
        if owned is not None:
            owned.ready = True
            if placement is None:
                owned.placement = actor.placement
        identity = await self._get(handle.identity.remote())
        if identity["sandbox_namespace"] != sandbox_namespace or identity["resource_spec"] != resources:
            raise SnapdResultUnknown("existing actor belongs to a different sandbox launch")
        if placement is not None and (
            actor.placement.node_id != placement.node_id or actor.placement.host_ip != placement.host_ip
        ):
            raise SnapdResultUnknown("Ray actor moved away from the selected snapd worker")
        return actor

    async def _sandbox_target(
        self, sandbox_id: str, runtime: SnapdSandboxRuntime | None
    ) -> tuple[_SandboxActor | None, SnapdPlacement | None]:
        if runtime is not None and runtime.sandbox_id != sandbox_id:
            raise ValueError("runtime belongs to a different sandbox")
        actor = await self._find_actor(sandbox_id)
        return actor, actor.placement if actor is not None else (runtime.placement if runtime else None)

    @staticmethod
    def _observation(
        sandbox_id: str, placement: SnapdPlacement | None, code: OperatorResultCode, state: str, **kwargs: Any
    ) -> SandboxInfo:
        return sandbox_info_from_result(
            OperatorSandboxResult(
                sandbox_id,
                code,
                state,
                target=(
                    OperatorTarget(placement.node_id, placement.host_ip, placement.host_name)
                    if placement is not None
                    else OperatorTarget()
                ),
                **kwargs,
            )
        )

    def _lifecycle_error(
        self, sandbox_id: str, placement: SnapdPlacement | None, message: str, *, definitive: bool = False
    ) -> OperatorLifecycleError:
        return OperatorLifecycleError(
            self._observation(
                sandbox_id,
                placement,
                OperatorResultCode.FAILED if definitive else OperatorResultCode.RESULT_UNKNOWN,
                "FAILED" if definitive else "PENDING",
                last_error=message,
            )
        )

    async def _runtime_status(self, sandbox_id: str, placement: SnapdPlacement) -> SandboxStatusResult | None:
        try:
            result = await self._snapd_clients.client(placement.host_ip).get_sandbox_status(
                sandbox_id, deadline_unix_ms=self._deadline()
            )
        except SnapdAPIError as error:
            if error.code == "NOT_FOUND":
                return None
            raise
        if result.sandbox_id != sandbox_id:
            raise SnapdResultUnknown("snapd status reply belongs to another sandbox")
        return result

    def _status_info(self, sandbox_id: str, placement: SnapdPlacement, result: SandboxStatusResult) -> SandboxInfo:
        code = {
            "RUNNING": OperatorResultCode.RUNNING,
            "STOPPED": OperatorResultCode.STOPPED,
            "FAILED": OperatorResultCode.FAILED,
        }.get(result.state, OperatorResultCode.ACCEPTED)
        return self._observation(
            sandbox_id,
            placement,
            code,
            result.state,
            last_error=result.last_error,
            metadata={"snapd_state": result.state},
            guest_endpoint=(
                advertise_guest_endpoint(result.guest_endpoint, placement.host_ip)
                if result.guest_endpoint is not None
                else None
            ),
            vmm_pid=result.vmm_pid,
            rocklet_artifact=(
                result.rocklet_artifact.model_dump(mode="json") if result.rocklet_artifact is not None else None
            ),
        )

    async def _start_sandbox(self, actor: _SandboxActor, runtime: SnapdSandboxRuntime) -> SandboxInfo:
        sandbox_id, placement = actor.sandbox_id, actor.placement
        previous = await self._runtime_status(sandbox_id, placement)
        if previous is not None and previous.state in {"STARTING", "RUNNING"}:
            return self._status_info(sandbox_id, placement, previous)
        canonical_spec = json.dumps(
            runtime.snapshot_spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        request = StartSandboxFromSnapshotRequest(
            sandbox_id=sandbox_id,
            snapshot_spec=runtime.snapshot_spec,
            spec_checksum="sha256:" + hashlib.sha256(canonical_spec).hexdigest(),
            resource_spec=_snapd_resource_spec(runtime.resource_spec),
            p2p_source_worker_ip=runtime.source_worker_ip if runtime.source_worker_ip != placement.host_ip else None,
        )
        try:
            result = await self._snapd_clients.client(placement.host_ip).start_sandbox_from_snapshot_async(
                runtime.source_snapshot_id, request, deadline_unix_ms=self._deadline()
            )
        except SnapdAPIError as error:
            # A conflicting request may already own a VM; keep its resources.
            raise self._lifecycle_error(
                sandbox_id, placement, str(error), definitive=error.code != "IDENTITY_CONFLICT"
            ) from error
        if result.identity != sandbox_id or result.state != "STARTING":
            raise SnapdResultUnknown("snapd async start reply does not match its sandbox or STARTING admission")
        return self._observation(
            sandbox_id,
            placement,
            OperatorResultCode.ACCEPTED,
            "PENDING",
            metadata={"snapd_state": "STARTING", "snapd_node_id": placement.node_id},
        )

    async def _stop_sandbox(self, sandbox_id: str, placement: SnapdPlacement, reason: StopReason) -> None:
        deadline = self._deadline()
        await self._snapd_clients.client(placement.host_ip).stop_sandbox(
            sandbox_id, StopSandboxRequest(reason=reason.value, deadline_unix_ms=deadline), deadline_unix_ms=deadline
        )

    async def _kill_actor(self, actor: _SandboxActor) -> None:
        await asyncio.to_thread(ray.kill, actor.handle, no_restart=True)
        deadline = time.monotonic() + min(10, self._request_timeout_ms / 1000)
        while time.monotonic() < deadline:
            current = await self._ray_service.async_try_get_actor(self._actor_name(actor.sandbox_id))
            if current is None:
                return
            try:
                identity = await self._get(current.identity.remote())
            except Exception:
                await asyncio.sleep(0.05)
                continue
            if identity.get("actor_generation") != actor.generation:
                return
            await asyncio.sleep(0.05)
        raise self._lifecycle_error(actor.sandbox_id, actor.placement, "Ray actor resource release is not confirmed")

    async def _abort_unstarted(self, actors: list[_SandboxActor]) -> None:
        async def abort(actor):
            if not actor.ready:
                await asyncio.to_thread(ray.kill, actor.handle, no_restart=True)
                return
            identity = await self._get(actor.handle.identity.remote())
            if identity.get("actor_generation") != actor.generation:
                raise SnapdResultUnknown("actor creation ownership changed")
            # Only called before this request dispatches a launch. Confirm the
            # daemon has no active VM before releasing the owned reservation.
            status = await self._runtime_status(actor.sandbox_id, actor.placement)
            if status is not None and status.state not in {"STOPPED", "FAILED"}:
                raise SnapdResultUnknown("sandbox became active before dispatch cleanup")
            await self._kill_actor(actor)

        results = await asyncio.gather(*(abort(actor) for actor in actors), return_exceptions=True)
        failure = next((result for result in results if isinstance(result, BaseException)), None)
        if failure is not None:
            raise SnapdResultUnknown("pre-dispatch actor cleanup is unknown") from failure

    def _deadline(self) -> int:
        return int(time.time() * 1000) + self._request_timeout_ms

    @staticmethod
    def _error_code(error: SnapdAPIError) -> str:
        return getattr(error.code, "value", str(error.code))

    async def resolve_base_image(self, request: Any) -> SnapshotOperationResult:
        from rock.sandbox.operator.snapd_actor import select_snapd_worker

        async def resolve():
            try:
                async with self._ray_service.get_ray_rwlock().read_lock():
                    reference = select_snapd_worker.remote()
            except Exception as error:
                raise SnapdResultUnknown(f"Ray BASE_IMAGE resolution dispatch failed: {error}") from error
            worker_ip = await self._get(reference)
            resolved = await self._snapd_clients.client(worker_ip).resolve_base_image(
                request, deadline_unix_ms=self._deadline()
            )
            return {**resolved.model_dump(mode="json"), "source_worker_ip": worker_ip}

        result = await self._snapshot_call(
            "base-image-resolution",
            resolve,
        )
        build_key = result.payload.get("base_image_build_key")
        return SnapshotOperationResult(
            build_key if isinstance(build_key, str) else result.snapshot_id,
            result.code,
            result.state,
            result.payload,
            result.last_error,
        )

    async def create_base_image(
        self,
        request: Any,
        *,
        source_worker_ip: str,
    ) -> SnapshotOperationResult:
        result = await self._snapshot_call(
            request.snapshot_id,
            lambda: self._snapd_clients.client(source_worker_ip).create_base_image(
                request,
                deadline_unix_ms=self._deadline(),
            ),
        )
        return SnapshotOperationResult(
            result.snapshot_id,
            result.code,
            result.state,
            {
                **result.payload,
                "source_worker_ip": source_worker_ip,
            },
            result.last_error,
        )

    async def _source_actor(self, source: SnapshotSource) -> _SandboxActor:
        actor = await self._find_actor(source.sandbox_id)
        if actor is None:
            raise SnapdResultUnknown("snapshot source has no live execution actor")
        if actor.placement.node_id != source.target.node_id or actor.placement.host_ip != source.target.host_ip:
            raise SnapdResultUnknown("snapshot source actor differs from durable placement")
        return actor

    async def capture_snapshot(self, source: SnapshotSource, request: Any) -> SnapshotOperationResult:
        actor = await self._source_actor(source)
        return await self._snapshot_call(
            request.snapshot_id,
            lambda: self._snapd_clients.client(actor.placement.host_ip).snapshot_sandbox(
                source.sandbox_id, request, deadline_unix_ms=self._deadline()
            ),
        )

    async def resolve_source(self, source: SnapshotSource) -> SnapshotSource:
        actor = await self._source_actor(source)
        result = await self._runtime_status(source.sandbox_id, actor.placement)
        if result is None or result.sandbox_id != source.sandbox_id or result.state != "RUNNING":
            raise SnapdResultUnknown("snapshot source is not a verified RUNNING sandbox")
        return source

    async def fork_snapshot(self, source: SnapshotSource, request: Any) -> ForkOperationResult:
        child_ids = [child.sandbox_id for child in request.children]
        if not child_ids or len(child_ids) > MAX_FORK_CHILDREN or len(child_ids) != len(set(child_ids)):
            return ForkOperationResult(
                request.snapshot_id,
                OperatorResultCode.IDENTITY_CONFLICT,
                "FAILED",
                last_error="fork requires 1..32 unique child IDs",
            )
        source_actor = await self._source_actor(source)
        placement = source_actor.placement
        created: dict[str, list[_SandboxActor]] = {sandbox_id: [] for sandbox_id in child_ids}
        outcomes: dict[str, dict[str, Any]] = {}
        dispatched = False

        def unknown_result(message, payload=None):
            children = []
            for sandbox_id in child_ids:
                outcome = dict(outcomes.get(sandbox_id, {"sandbox_id": sandbox_id, "state": "PENDING"}))
                if outcome["state"] == "PENDING":
                    outcome.setdefault("last_error", message)
                children.append(outcome)
            return ForkOperationResult(
                request.snapshot_id,
                OperatorResultCode.RESULT_UNKNOWN,
                "CREATING",
                {**(payload or {}), "children": children},
                message,
                tuple(children),
            )

        async def prepare(child):
            try:
                self._actor_options(child.sandbox_id, child.resource_spec, placement)
            except (TypeError, ValueError) as error:
                outcomes[child.sandbox_id] = {
                    "sandbox_id": child.sandbox_id,
                    "state": "FAILED",
                    "last_error": str(error),
                }
                return None
            owned = created[child.sandbox_id]
            try:
                actor = await self._create_actor(
                    child.sandbox_id,
                    source.sandbox_namespace,
                    child.resource_spec,
                    placement,
                    created=owned,
                )
                return actor
            except Exception as error:
                state = "PENDING"
                if owned:
                    try:
                        await self._abort_unstarted(owned)
                    except Exception as cleanup_error:
                        error = SnapdResultUnknown(f"{error}; actor cleanup is unknown: {cleanup_error}")
                    else:
                        state = "FAILED"
                        owned.clear()
                outcomes[child.sandbox_id] = {
                    "sandbox_id": child.sandbox_id,
                    "state": state,
                    "last_error": str(error),
                }
                if owned and owned[0].placement is not None:
                    target = owned[0].placement
                    outcomes[child.sandbox_id]["placement"] = {
                        "node_id": target.node_id,
                        "host_ip": target.host_ip,
                        "host_name": target.host_name,
                    }
                return None

        tasks = [asyncio.create_task(prepare(child)) for child in request.children]
        try:
            ordered = await asyncio.gather(*tasks)
            admitted = [
                (actor, child) for actor, child in zip(ordered, request.children, strict=True) if actor is not None
            ]
            if not admitted:
                children = tuple(outcomes[sandbox_id] for sandbox_id in child_ids)
                return ForkOperationResult(
                    request.snapshot_id,
                    OperatorResultCode.FAILED,
                    "FAILED",
                    {"children": list(children)},
                    "no fork child actor was prepared",
                    children,
                )
            for actor, child in admitted:
                outcomes[child.sandbox_id] = {
                    "sandbox_id": child.sandbox_id,
                    "state": "PENDING",
                    "placement": {
                        "node_id": actor.placement.node_id,
                        "host_ip": actor.placement.host_ip,
                        "host_name": actor.placement.host_name,
                    },
                }
            dispatch_request = request.model_copy(
                update={
                    "children": [
                        child.model_copy(update={"resource_spec": _snapd_resource_spec(child.resource_spec)})
                        for _, child in admitted
                    ]
                }
            )
            dispatched = True
            result = await self._snapshot_call(
                request.snapshot_id,
                lambda: self._snapd_clients.client(placement.host_ip).fork_sandbox(
                    source.sandbox_id, dispatch_request, deadline_unix_ms=self._deadline()
                ),
            )
            if result.code is OperatorResultCode.RESULT_UNKNOWN:
                return unknown_result(result.last_error, result.payload)
            if result.code is OperatorResultCode.IDENTITY_CONFLICT:
                # Rejection does not prove that a previous dispatch created no VMs.
                # Keep the resource owners available for observation/explicit cleanup.
                return unknown_result(result.last_error, result.payload)
            payload_children = result.payload.get("children", ())
            if result.code is OperatorResultCode.FAILED and not payload_children:
                payload_children = [
                    {"sandbox_id": child.sandbox_id, "state": "FAILED", "last_error": result.last_error}
                    for _, child in admitted
                ]
            if [item.get("sandbox_id") for item in payload_children] != [child.sandbox_id for _, child in admitted]:
                raise SnapdResultUnknown("fork reply child identities differ from requested batch")

            async def finish(actor, child, payload):
                enriched = dict(payload)
                if "error" in enriched and "last_error" not in enriched:
                    enriched["last_error"] = enriched.pop("error")
                enriched["placement"] = {
                    "node_id": actor.placement.node_id,
                    "host_ip": actor.placement.host_ip,
                    "host_name": actor.placement.host_name,
                }
                try:
                    if enriched.get("state") == "RUNNING":
                        if enriched.get("guest_endpoint") is not None:
                            enriched["guest_endpoint"] = advertise_guest_endpoint(
                                enriched["guest_endpoint"],
                                actor.placement.host_ip,
                            )
                        attached = await self._runtime_status(child.sandbox_id, actor.placement)
                        if attached is None or attached.state != "RUNNING":
                            raise SnapdResultUnknown("snapd could not confirm the running fork child")
                    elif enriched.get("state") == "FAILED":
                        await self._stop_sandbox(child.sandbox_id, actor.placement, StopReason.MANUAL)
                        await self._kill_actor(actor)
                    else:
                        raise SnapdResultUnknown(enriched.get("last_error") or "fork child launch outcome is unknown")
                except Exception as error:
                    # A lost per-child observation must not hide successful siblings
                    # or release an actor that may still own a running VM.
                    enriched = {
                        "sandbox_id": child.sandbox_id,
                        "state": "PENDING",
                        "last_error": str(error),
                        "placement": enriched["placement"],
                    }
                outcomes[child.sandbox_id] = enriched

            await asyncio.gather(
                *(
                    finish(actor, child, payload)
                    for (actor, child), payload in zip(admitted, payload_children, strict=True)
                )
            )
            children = tuple(outcomes[sandbox_id] for sandbox_id in child_ids)
            return ForkOperationResult(
                result.snapshot_id,
                result.code,
                result.state,
                {**result.payload, "children": list(children)},
                result.last_error,
                children,
            )
        except BaseException as error:
            if not dispatched:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                cleanup = asyncio.create_task(
                    self._abort_unstarted(
                        [actor for owned in created.values() for actor in owned],
                    )
                )
                try:
                    await asyncio.shield(cleanup)
                except Exception as cleanup_error:
                    if not isinstance(error, asyncio.CancelledError):
                        error = SnapdResultUnknown(f"{error}; actor cleanup is unknown: {cleanup_error}")
            if isinstance(error, asyncio.CancelledError):
                raise
            return unknown_result(str(error))

    async def get_snapshot_status(
        self,
        snapshot_id: str,
        *,
        source_worker_ip: str,
    ) -> SnapshotOperationResult:
        try:
            result = await self._snapd_clients.client(source_worker_ip).get_snapshot_status(
                snapshot_id,
                deadline_unix_ms=self._deadline(),
            )
        except SnapdResultUnknown as error:
            return SnapshotOperationResult(
                snapshot_id, OperatorResultCode.RESULT_UNKNOWN, "CREATING", last_error=str(error)
            )
        except SnapdAPIError as error:
            code = OperatorResultCode.NOT_FOUND if self._error_code(error) == "NOT_FOUND" else OperatorResultCode.FAILED
            return SnapshotOperationResult(snapshot_id, code, "FAILED", last_error=str(error))
        return SnapshotOperationResult(
            snapshot_id,
            OperatorResultCode.RUNNING if result.state == "REMOTE_READY" else OperatorResultCode.ACCEPTED,
            result.state,
            result.model_dump(mode="json"),
            result.last_error,
        )

    async def finalize_published_snapshot(
        self,
        snapshot_id: str,
        request: Any,
        *,
        source_worker_ip: str,
    ) -> SnapshotOperationResult:
        return await self._snapshot_call(
            snapshot_id,
            lambda: self._snapd_clients.client(source_worker_ip).finalize_published_snapshot(
                snapshot_id,
                request,
                deadline_unix_ms=self._deadline(),
            ),
        )

    async def aclose(self) -> None:
        await self._snapd_clients.aclose()

    async def _snapshot_call(
        self,
        snapshot_id: str,
        call: Callable[[], Awaitable[dict[str, Any] | None]],
    ) -> SnapshotOperationResult:
        try:
            payload = await call()
        except SnapdResultUnknown as error:
            return SnapshotOperationResult(
                snapshot_id, OperatorResultCode.RESULT_UNKNOWN, "CREATING", last_error=str(error)
            )
        except SnapdAPIError as error:
            code = (
                OperatorResultCode.IDENTITY_CONFLICT
                if self._error_code(error) == "IDENTITY_CONFLICT"
                else OperatorResultCode.FAILED
            )
            return SnapshotOperationResult(snapshot_id, code, "FAILED", last_error=str(error))
        if payload is None:
            payload = {}
        elif hasattr(payload, "model_dump"):
            payload = payload.model_dump(mode="json")
        else:
            payload = dict(payload)
        state = str(payload.get("state", "CREATING"))
        if state not in {"CREATING", "LOCAL_READY", "REMOTE_READY", "FAILED", "DELETED"}:
            state = "CREATING"
        code = (
            OperatorResultCode.FAILED
            if state == "FAILED"
            else OperatorResultCode.RUNNING
            if state == "REMOTE_READY"
            else OperatorResultCode.ACCEPTED
        )
        return SnapshotOperationResult(
            snapshot_id,
            code,
            state,  # type: ignore[arg-type]
            payload,
        )
