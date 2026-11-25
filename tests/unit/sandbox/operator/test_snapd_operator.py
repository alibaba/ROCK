from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.actions.sandbox.response import State
from rock.admin.snapd.client import SnapdAPIError, SnapdResultUnknown
from rock.admin.snapd.protocol import (
    ChildLaunchPlan,
    CreateBaseImageRequest,
    FinalizePublishedSnapshotRequest,
    ForkChildResult,
    ForkLocalReadyResult,
    ForkSandboxRequest,
    MutationAcceptedResult,
    ResolveBaseImageRequest,
    ResolvedBaseImageResult,
    RockletArtifactIdentity,
    SandboxStatusResult,
    SnapdErrorCode,
    SnapdGuestEndpoint,
    SnapdReply,
    SnapshotSandboxRequest,
    SnapshotStatusResult,
)
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.operator.snapd import (
    OperatorLifecycleError,
    OperatorResultCode,
    OperatorTarget,
    SnapdOperator,
    SnapdPlacement,
    SnapdSandboxRuntime,
    operator_result_from_info,
)
from rock.sandbox.operator.snapd_actor import SnapdSandboxActor
from rock.sandbox.operator.snapshot import AbstractSnapshotOperator
from rock.sandbox.snapshot_types import SnapshotSource

_ACTOR_CLASS = SnapdSandboxActor.__ray_metadata__.modified_class
_PLACEMENT = SnapdPlacement("node-a", "10.0.0.1", "node-a")
_ROCKLET = RockletArtifactIdentity(
    identity="sha256:" + "a" * 64,
    version="1.11.0-test",
    sha256="sha256:" + "a" * 64,
    logical_bytes=64 * 1024 * 1024,
)


def _base_request(snapshot_id: str) -> CreateBaseImageRequest:
    return CreateBaseImageRequest(
        snapshot_id=snapshot_id,
        oci_ref="registry.example/base:latest",
        oci_digest="sha256:" + "a" * 64,
        platform="linux/amd64",
        base_image_builder_version="test-builder-v1",
        base_image_build_key="sha256:" + "b" * 64,
    )


def _endpoint(index: int = 0) -> SnapdGuestEndpoint:
    return SnapdGuestEndpoint(
        transport="vsock-tcp-forward",
        host_port=32_000 + index,
        guest_port=22_555,
        vsock_cid=10_000 + index,
    )


def _api_error(code: SnapdErrorCode, message: str) -> SnapdAPIError:
    return SnapdAPIError(SnapdReply(request_id="test-request", code=code, error_message=message))


class _SnapdClient:
    """Node-local fake that tracks individual VM incarnations, not actor state."""

    def __init__(self):
        self.calls = []
        self.runtimes = {}
        self.start_error = None
        self.start_reply_mismatch = False
        self.status_error = None
        self.stop_error = None
        self.delete_error = None
        self.fork_error = None
        self.fork_transform = None
        self.before_status = None
        self.before_stop = None

    def runtime(self, sandbox_id, state="RUNNING", index=0):
        result = SandboxStatusResult(
            sandbox_id=sandbox_id,
            state=state,
            guest_endpoint=_endpoint(index) if state == "RUNNING" else None,
            vmm_pid=4242 + index if state == "RUNNING" else None,
            rocklet_artifact=_ROCKLET if state == "RUNNING" else None,
        )
        self.runtimes[sandbox_id] = result
        return result

    async def healthcheck(self):
        return True

    async def get_sandbox_status(self, sandbox_id, *, deadline_unix_ms):
        self.calls.append(("status", sandbox_id, deadline_unix_ms))
        if self.before_status:
            await self.before_status(sandbox_id)
        if self.status_error:
            raise self.status_error
        result = self.runtimes.get(sandbox_id)
        if result is None:
            raise _api_error(SnapdErrorCode.NOT_FOUND, "no such runtime")
        return result

    async def start_sandbox_from_snapshot_async(self, snapshot_id, request, *, deadline_unix_ms):
        self.calls.append(("start", snapshot_id, request, deadline_unix_ms))
        if self.start_error:
            raise self.start_error
        self.runtime(request.sandbox_id, "STARTING")
        return MutationAcceptedResult(
            identity="another-sandbox" if self.start_reply_mismatch else request.sandbox_id,
            state="STARTING",
        )

    async def stop_sandbox(self, sandbox_id, request, *, deadline_unix_ms):
        self.calls.append(("stop", sandbox_id, request, deadline_unix_ms))
        if self.before_stop:
            await self.before_stop(sandbox_id)
        if self.stop_error:
            raise self.stop_error
        self.runtime(sandbox_id, "STOPPED")
        return {"sandbox_id": sandbox_id, "state": "STOPPED"}

    async def delete_sandbox(self, sandbox_id, request, *, deadline_unix_ms):
        self.calls.append(("delete", sandbox_id, request, deadline_unix_ms))
        if self.delete_error:
            raise self.delete_error
        self.runtimes.pop(sandbox_id, None)
        return {"sandbox_id": sandbox_id, "state": "STOPPED"}

    async def fork_sandbox(self, sandbox_id, request, *, deadline_unix_ms):
        self.calls.append(("fork", sandbox_id, request, deadline_unix_ms))
        if self.fork_error:
            raise self.fork_error
        children = []
        for index, child in enumerate(request.children):
            runtime = self.runtime(child.sandbox_id, "RUNNING", index)
            children.append(
                ForkChildResult(
                    sandbox_id=child.sandbox_id,
                    state="RUNNING",
                    guest_endpoint=runtime.guest_endpoint,
                    vmm_pid=runtime.vmm_pid,
                    rocklet_artifact=_ROCKLET,
                )
            )
        result = ForkLocalReadyResult(snapshot_id=request.snapshot_id, state="LOCAL_READY", children=children)
        return self.fork_transform(result) if self.fork_transform else result

    async def snapshot_sandbox(self, sandbox_id, request, *, deadline_unix_ms):
        self.calls.append(("snapshot", sandbox_id, request, deadline_unix_ms))
        return {"snapshot_id": request.snapshot_id, "state": "LOCAL_READY"}


class _SnapshotOwnerClient:
    def __init__(self, *, owns_snapshot: bool, observation_failure: BaseException | None = None) -> None:
        self.owns_snapshot = owns_snapshot
        self.observation_failure = observation_failure
        self.calls: list[str] = []

    async def get_snapshot_status(self, snapshot_id, *, deadline_unix_ms):
        del deadline_unix_ms
        self.calls.append(f"status:{snapshot_id}")
        if self.observation_failure is not None:
            raise self.observation_failure
        if not self.owns_snapshot:
            raise SnapdAPIError(
                SnapdReply(
                    request_id="test-request",
                    code=SnapdErrorCode.NOT_FOUND,
                    error_message="snapshot is absent",
                )
            )
        return SnapshotStatusResult(snapshot_id=snapshot_id, state="REMOTE_READY", progress=1.0)

    async def create_base_image(self, request, *, deadline_unix_ms):
        del deadline_unix_ms
        self.calls.append(f"create:{request.snapshot_id}")
        self.owns_snapshot = True
        return {"snapshot_id": request.snapshot_id, "state": "CREATING"}

    async def resolve_base_image(self, request, *, deadline_unix_ms):
        del deadline_unix_ms
        self.calls.append(f"resolve:{request.oci_ref}")
        return ResolvedBaseImageResult(
            oci_ref=request.oci_ref,
            oci_digest="sha256:" + "a" * 64,
            oci_platform=request.platform,
            base_image_builder_version="test-builder-v1",
            base_image_build_key="sha256:" + "b" * 64,
        )

    async def finalize_published_snapshot(self, snapshot_id, request, *, deadline_unix_ms):
        del request, deadline_unix_ms
        self.calls.append(f"finalize:{snapshot_id}")
        return {"identity": snapshot_id, "state": "REMOTE_READY", "released_pins": 1}


class _SnapshotClientPool:
    def __init__(self, *clients: _SnapshotOwnerClient) -> None:
        self.clients = {f"10.0.0.{index}": client for index, client in enumerate(clients, start=1)}
        self.calls = []

    def client(self, host_ip: str):
        self.calls.append(host_ip)
        return self.clients[host_ip]


class _Lock:
    @asynccontextmanager
    async def read_lock(self):
        yield


class _Call:
    def __init__(self, handle, method, args, kwargs):
        self.handle, self.method, self.args, self.kwargs = handle, method, args, kwargs


class _Method:
    def __init__(self, handle, name):
        self.handle, self.name = handle, name

    def remote(self, *args, **kwargs):
        return _Call(self.handle, self.name, args, kwargs)


class _Handle:
    def __init__(self, instance, options):
        self.instance, self.options = instance, options
        self.dead = False

    def __getattr__(self, name):
        return _Method(self, name)


class _Builder:
    def __init__(self, service, options):
        self.service, self.options_value = service, options

    def remote(self, *args, **kwargs):
        name = self.options_value.get("name")
        if name in self.service.registry:
            raise ValueError("actor name is already taken")
        with (
            patch("rock.sandbox.operator.snapd_actor.ray.get_runtime_context") as context,
            patch(
                "rock.sandbox.operator.snapd_actor.ray.util.get_node_ip_address",
                return_value=self.service.actor_host_ip,
            ),
        ):
            context.return_value.get_node_id.return_value = self.service.actor_node_id
            instance = _ACTOR_CLASS(*args, **kwargs)
        handle = _Handle(instance, self.options_value)
        self.service.created.append(handle)
        if name:
            self.service.registry[name] = handle
        return handle


class _RayService:
    def __init__(self):
        self.registry = {}
        self.created = []
        self.killed = []
        self.calls = []
        self.before_call = None
        self.actor_node_id = "node-a"
        self.actor_host_ip = "10.0.0.1"

    def options(self, **options):
        return _Builder(self, options)

    def get_ray_rwlock(self):
        return _Lock()

    async def async_try_get_actor(self, name):
        return self.registry.get(name)

    async def async_ray_get(self, reference, timeout=60):
        del timeout
        self.calls.append(reference)
        if self.before_call:
            await self.before_call(reference)
        if reference.handle.dead:
            raise RuntimeError("actor is dead")
        result = getattr(reference.handle.instance, reference.method)(*reference.args, **reference.kwargs)
        return await result if inspect.isawaitable(result) else result

    def kill(self, handle, *, no_restart):
        assert no_restart is True
        handle.dead = True
        self.killed.append(handle)
        name = handle.options.get("name")
        if name and self.registry.get(name) is handle:
            del self.registry[name]


class _Harness:
    def __init__(self, monkeypatch):
        self.service = _RayService()
        self.client = _SnapdClient()
        self.runtime = SnapdSandboxRuntime(
            sandbox_id="vm-1",
            source_snapshot_id="snap-1",
            sandbox_namespace="tenant-a",
            placement=_PLACEMENT,
            resource_spec={"vcpus": 2, "memory_mib": 512},
            snapshot_spec={"backend_payload": {"value": "opaque"}},
            source_worker_ip="10.0.0.9",
        )
        self.client_urls = []
        monkeypatch.setattr("rock.sandbox.operator.snapd_actor.SnapdSandboxActor", self.service)
        monkeypatch.setattr("rock.sandbox.operator.snapd.ray.kill", self.service.kill)
        monkeypatch.setattr("rock.sandbox.operator.snapd.ray.is_initialized", lambda: False)
        constructor = MagicMock(side_effect=self.make_client)
        monkeypatch.setattr("rock.admin.snapd.worker_client.SnapdClient", constructor)
        self.operator = SnapdOperator(self.service)

    def make_client(self, url):
        self.client_urls.append(url)
        return self.client

    async def seed(self, sandbox_id="vm-1", *, state="RUNNING"):
        self.client.runtime(sandbox_id, state)
        return await self.operator._create_actor(
            sandbox_id,
            "tenant-a",
            {"vcpus": 2, "memory_mib": 512},
            _PLACEMENT,

        )

    async def source(self):
        await self.seed("source")
        return _snapshot_source()


@pytest.fixture
def harness(monkeypatch):
    return _Harness(monkeypatch)


def _config():
    return DockerDeploymentConfig(
        image="snapshot:snap-1",
        source_snapshot_id="snap-1",
        container_name="vm-1",
        cpus=2,
        memory="512M",
    )


def _snapshot_source():
    return SnapshotSource(
        sandbox_id="source",
        sandbox_namespace="tenant-a",
        target=OperatorTarget(node_id="node-a", host_ip="10.0.0.1", host_name="node-a"),
    )


def _fork_request(count=4):
    return ForkSandboxRequest(
        snapshot_id="fork-snapshot",
        children=[
            ChildLaunchPlan(
                sandbox_id=f"child-{index}",
                resource_spec={"vcpus": 1, "memory_mib": 128},
            )
            for index in range(count)
        ],
    )


def test_snapd_operator_is_an_explicit_lifecycle_and_snapshot_operator():
    assert issubclass(SnapdOperator, AbstractOperator)
    assert issubclass(SnapdOperator, AbstractSnapshotOperator)
    for name in ("submit_command", "get_status_command", "restart_command", "stop_command", "delete_command"):
        assert not hasattr(AbstractOperator, name)
        assert not hasattr(SnapdOperator, name)
    assert not hasattr(SnapdOperator, "_lifecycle_inputs")
    assert not hasattr(SnapdOperator, "_lifecycle_info")
    assert "repository" not in inspect.signature(SnapdOperator).parameters
    assert not hasattr(SnapdOperator, "_runtime_repository")


@pytest.mark.asyncio
@pytest.mark.parametrize(("cpus", "wire_cpus"), [(1, 1), (2, 2), (2.25, 3)])
async def test_submit_uses_ray_placement_then_admin_sends_snapd_start(harness, cpus, wire_cpus):
    resources = {"vcpus": cpus, "memory_mib": 512}
    harness.runtime = replace(harness.runtime, placement=None, resource_spec=resources)
    harness.service.actor_node_id = "ray-selected-node"
    harness.service.actor_host_ip = "10.0.0.2"
    info = await harness.operator.submit(_config(), {"namespace": "ignored-user-input"}, runtime=harness.runtime)

    result = operator_result_from_info(info)
    assert result.code is OperatorResultCode.ACCEPTED
    assert info["state"] is State.PENDING
    assert result.target.node_id == "ray-selected-node"
    assert result.target.host_ip == "10.0.0.2"
    assert info["host_ip"] == "10.0.0.2"
    (actor,) = harness.service.created
    assert actor.options["num_cpus"] == wire_cpus
    assert actor.options["memory"] == 512 * 1024**2
    assert actor.options["resources"] == {}
    assert actor.options["lifetime"] == "detached"
    assert "get_if_exists" not in actor.options
    assert actor.instance.identity()["sandbox_namespace"] == "tenant-a"
    assert actor.instance.identity()["resource_spec"] == resources
    assert harness.client_urls == ["http://10.0.0.2:22556"]
    assert {call.method for call in harness.service.calls} <= {"identity", "placement"}
    assert "scheduling_strategy" not in actor.options
    (start,) = [call for call in harness.client.calls if call[0] == "start"]
    assert start[1] == "snap-1"
    assert start[2].snapshot_spec == {"backend_payload": {"value": "opaque"}}
    assert start[2].resource_spec == {"cpu": wire_cpus, "memory_mib": 512}
    assert start[2].p2p_source_worker_ip == "10.0.0.9"
    assert not hasattr(harness.operator, "_repository")
    assert not harness.service.killed


@pytest.mark.parametrize("placement", [None, _PLACEMENT])
def test_actor_disk_resources_do_not_force_a_node_for_new_sandboxes(harness, placement):
    harness.operator._disk_scheduling_enabled = True
    options = harness.operator._actor_options("vm-1", {"vcpus": 2, "memory_mib": 512, "disk": "1G"}, placement)
    assert options["num_cpus"] == 2
    assert options["memory"] == 512 * 1024**2
    assert options["resources"] == {"disk": 1024**3}
    if placement is None:
        assert "scheduling_strategy" not in options
    else:
        assert options["scheduling_strategy"].node_id == placement.node_id
        assert options["scheduling_strategy"].soft is False


@pytest.mark.asyncio
@pytest.mark.parametrize("observe_placement", [False, True])
async def test_new_actor_admission_failure_retains_only_observed_placement(harness, observe_placement):
    harness.runtime = replace(harness.runtime, placement=None)
    identity_calls = 0

    async def fail(reference):
        nonlocal identity_calls
        if reference.method == "identity":
            identity_calls += 1
            if observe_placement and identity_calls == 2:
                raise RuntimeError("launch identity reply failed")
        elif reference.method == "placement" and not observe_placement:
            raise RuntimeError("Ray placement reply failed")

    harness.service.before_call = fail
    with pytest.raises(OperatorLifecycleError) as exc:
        await harness.operator.submit(_config(), runtime=harness.runtime)
    target = operator_result_from_info(exc.value.sandbox_info).target
    assert target == (OperatorTarget("node-a", "10.0.0.1", "node-a") if observe_placement else OperatorTarget())
    assert harness.service.created[0].dead
    assert not [call for call in harness.client.calls if call[0] != "status"]


@pytest.mark.asyncio
async def test_pending_submit_actor_is_killed_without_waiting_for_actor_methods(harness):
    harness.runtime = replace(harness.runtime, placement=None)
    attempted_methods = []

    async def no_capacity(reference):
        attempted_methods.append(reference.method)
        raise TimeoutError("actor is waiting for Ray resources")

    harness.service.before_call = no_capacity
    with pytest.raises(OperatorLifecycleError, match="waiting for Ray resources"):
        await harness.operator.submit(_config(), runtime=harness.runtime)

    (actor,) = harness.service.created
    assert actor.dead
    assert harness.service.killed == [actor]
    assert not harness.service.registry
    assert sorted(attempted_methods) == ["identity", "placement"]
    assert not harness.client.calls

    assert await harness.operator.stop("vm-1", runtime=harness.runtime) is True
    assert await harness.operator.delete(_config(), runtime=harness.runtime) is True
    harness.service.before_call = None
    assert await harness.operator.get_status("vm-1", runtime=harness.runtime) is None
    assert harness.service.created == [actor]
    assert not harness.client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_status", "stop", "delete"])
async def test_existing_actor_lifecycle_needs_no_persisted_runtime(harness, operation):
    actor = await harness.seed()
    if operation == "delete":
        result = await harness.operator.delete(_config(), host_ip="10.0.0.1")
    else:
        result = await getattr(harness.operator, operation)("vm-1")
    if operation == "get_status":
        assert result["state"] is State.RUNNING
        assert result["port_mapping"][22555] == 32000
        assert not actor.handle.dead
    else:
        assert result is True
        assert actor.handle.dead
    assert len(harness.service.created) == 1


@pytest.mark.asyncio
async def test_submit_unknown_keeps_actor_without_actor_local_retry_policy(harness):
    harness.runtime = replace(harness.runtime, placement=None)
    harness.client.start_error = SnapdResultUnknown("reply was lost")
    errors = []
    for _ in range(2):
        with pytest.raises(OperatorLifecycleError) as exc:
            await harness.operator.submit(_config(), runtime=harness.runtime)
        errors.append(operator_result_from_info(exc.value.sandbox_info))
    assert all(error.result_unknown for error in errors)

    assert errors[0].target == errors[1].target
    assert len([call for call in harness.client.calls if call[0] == "start"]) == 2
    assert len(harness.service.created) == 1
    assert not harness.service.killed


@pytest.mark.asyncio
async def test_submit_ack_loss_replay_observes_existing_runtime_without_redispatch(harness):
    harness.client.start_error = SnapdResultUnknown("reply was lost")
    with pytest.raises(OperatorLifecycleError):
        await harness.operator.submit(_config(), runtime=harness.runtime)
    (actor,) = harness.service.created
    harness.client.runtime("vm-1", "RUNNING")

    info = await harness.operator.submit(_config(), runtime=harness.runtime)

    assert info["state"] is State.RUNNING
    assert len([call for call in harness.client.calls if call[0] == "start"]) == 1
    assert len(harness.service.created) == 1


async def test_missing_status_after_unknown_start_keeps_resource_owner(harness):
    harness.client.start_error = SnapdResultUnknown("reply was lost")
    with pytest.raises(OperatorLifecycleError):
        await harness.operator.submit(_config(), runtime=harness.runtime)

    assert await harness.operator.get_status("vm-1") is None
    assert not harness.service.created[0].dead
    assert not harness.service.killed


@pytest.mark.asyncio
async def test_definitive_start_failure_releases_resource_actor(harness):
    harness.client.start_error = _api_error(SnapdErrorCode.INVALID_ARGUMENT, "invalid snapshot")
    with pytest.raises(OperatorLifecycleError) as exc:
        await harness.operator.submit(_config(), runtime=harness.runtime)
    assert operator_result_from_info(exc.value.sandbox_info).code is OperatorResultCode.FAILED
    assert len(harness.service.killed) == 1
    assert not harness.service.registry


@pytest.mark.asyncio
async def test_mismatched_start_reply_preserves_unknown_actor(harness):
    harness.client.start_reply_mismatch = True
    with pytest.raises(OperatorLifecycleError) as exc:
        await harness.operator.submit(_config(), runtime=harness.runtime)
    assert operator_result_from_info(exc.value.sandbox_info).result_unknown
    assert not harness.service.killed


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["STOPPED", "FAILED"])
async def test_terminal_status_releases_same_actor(harness, state):
    actor = await harness.seed(state=state)
    info = await harness.operator.get_status("vm-1")
    assert operator_result_from_info(info).state == state
    assert harness.service.killed == [actor.handle]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_status", "stop", "delete"])
async def test_unknown_lifecycle_outcome_does_not_release_resources(harness, operation):
    actor = await harness.seed()
    error = SnapdResultUnknown("node reply was lost")
    setattr(
        harness.client, {"get_status": "status_error", "stop": "stop_error", "delete": "delete_error"}[operation], error
    )
    with pytest.raises(OperatorLifecycleError) as exc:
        if operation == "delete":
            await harness.operator.delete(_config())
        else:
            await getattr(harness.operator, operation)("vm-1")
    assert operator_result_from_info(exc.value.sandbox_info).result_unknown
    assert not actor.handle.dead
    assert not harness.service.killed


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_status", "stop", "delete"])
async def test_missing_actor_uses_durable_placement_without_creating_recovery_actor(harness, operation):
    harness.client.runtime("vm-1", state="STOPPED" if operation == "get_status" else "RUNNING")
    runtime = replace(harness.runtime, snapshot_spec=None, source_worker_ip=None)
    if operation == "delete":
        await harness.operator.delete(_config(), host_ip="10.0.0.1", runtime=runtime)
    else:
        await getattr(harness.operator, operation)("vm-1", runtime=runtime)
    assert not harness.service.created
    assert harness.client_urls == ["http://10.0.0.1:22556"]
    assert not [call for call in harness.client.calls if call[0] == "start"]
    assert not harness.service.registry


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["STARTING", "RUNNING"])
async def test_orphan_active_vm_status_is_unknown_and_does_not_mutate_vm(harness, state):
    runtime = harness.client.runtime("vm-1", state=state)

    with pytest.raises(OperatorLifecycleError, match="resource-owning actor is gone") as exc:
        await harness.operator.get_status("vm-1", runtime=harness.runtime)

    result = operator_result_from_info(exc.value.sandbox_info)
    assert result.result_unknown
    assert result.target.host_ip == "10.0.0.1"
    assert [call[0] for call in harness.client.calls] == ["status"]
    assert harness.client.runtimes["vm-1"] == runtime
    assert not harness.service.created


@pytest.mark.asyncio
async def test_orphan_absent_vm_status_needs_no_probe_actor(harness):
    assert await harness.operator.get_status("vm-1", runtime=harness.runtime) is None
    assert not harness.service.created
    assert [call[0] for call in harness.client.calls] == ["status"]


@pytest.mark.asyncio
async def test_unplaced_pending_sandbox_cleanup_never_creates_or_starts_actor(harness):
    harness.runtime = replace(harness.runtime, placement=None)
    assert await harness.operator.stop("vm-1", runtime=harness.runtime) is True
    assert await harness.operator.delete(_config(), runtime=harness.runtime) is True
    assert not harness.service.created
    assert not harness.client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_status", "stop", "delete"])
async def test_missing_actor_without_recovery_data_does_not_probe_workers(harness, operation):
    if operation == "delete":
        result = await harness.operator.delete(_config())
    else:
        result = await getattr(harness.operator, operation)("vm-1")
    assert result is (None if operation == "get_status" else True)
    assert not harness.service.created
    assert not harness.client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_status", "stop", "delete"])
async def test_recovery_data_for_another_sandbox_is_rejected_before_dispatch(harness, operation):
    runtime = replace(harness.runtime, sandbox_id="different-sandbox")
    with pytest.raises(ValueError, match="different sandbox"):
        if operation == "delete":
            await harness.operator.delete(_config(), runtime=runtime)
        else:
            await getattr(harness.operator, operation)("vm-1", runtime=runtime)
    assert not harness.service.created
    assert not harness.client.calls


@pytest.mark.asyncio
async def test_delete_wrong_host_is_rejected_without_mutation_or_actor_release(harness):
    actor = await harness.seed()
    with pytest.raises(OperatorLifecycleError):
        await harness.operator.delete(_config(), host_ip="10.0.0.99")
    assert not harness.client.calls
    assert not actor.handle.dead


@pytest.mark.asyncio
async def test_wrong_durable_node_is_rejected_before_vm_dispatch(harness):
    harness.runtime = replace(
        harness.runtime,
        placement=SnapdPlacement("missing-node", "10.0.0.1"),
    )
    with pytest.raises(OperatorLifecycleError, match="selected snapd worker") as exc:
        await harness.operator.submit(_config(), runtime=harness.runtime)
    assert operator_result_from_info(exc.value.sandbox_info).code is OperatorResultCode.RESULT_UNKNOWN
    (actor,) = harness.service.created
    assert actor.options["scheduling_strategy"].node_id == "missing-node"
    assert actor.options["scheduling_strategy"].soft is False
    assert actor.dead
    assert harness.service.killed == [actor]
    assert not [call for call in harness.client.calls if call[0] != "status"]


@pytest.mark.asyncio
async def test_submit_requires_prepared_snapshot_spec_before_actor_or_vm_mutation(harness):
    harness.runtime = replace(harness.runtime, snapshot_spec=None)
    with pytest.raises(ValueError, match="snapshot"):
        await harness.operator.submit(_config(), runtime=harness.runtime)
    assert not harness.service.created
    assert not harness.client.calls


@pytest.mark.asyncio
async def test_submit_without_manager_runtime_never_reads_catalog_or_starts_actor(harness):
    with pytest.raises(ValueError, match="Manager-resolved snapshot runtime"):
        await harness.operator.submit(_config())
    assert not harness.service.created
    assert not harness.client.calls


@pytest.mark.asyncio
async def test_submit_with_mismatched_sandbox_runtime_is_rejected(harness):
    runtime = replace(harness.runtime, sandbox_id="different-sandbox")
    with pytest.raises(ValueError, match="sandbox identity"):
        await harness.operator.submit(_config(), runtime=runtime)
    assert not harness.service.created
    assert not harness.client.calls


@pytest.mark.asyncio
async def test_restart_reuses_durable_node_and_starts_new_actor(harness):
    harness.client.runtime("vm-1", "STOPPED")
    info = await harness.operator.restart(_config(), host_ip="10.0.0.1", runtime=harness.runtime)
    assert info["state"] is State.PENDING

    (actor,) = harness.service.created
    assert actor.options["resources"] == {}
    assert actor.options["scheduling_strategy"].node_id == "node-a"
    assert actor.options["scheduling_strategy"].soft is False


@pytest.mark.asyncio
async def test_restart_replaces_terminal_actor_before_start(harness):
    previous = await harness.seed(state="STOPPED")

    info = await harness.operator.restart(_config(), host_ip="10.0.0.1", runtime=harness.runtime)

    assert info["state"] is State.PENDING
    assert harness.service.killed == [previous.handle]
    assert len(harness.service.created) == 2
    assert harness.service.created[-1] is not previous.handle


@pytest.mark.asyncio
async def test_named_actor_creation_race_reuses_matching_existing_actor(harness, monkeypatch):
    existing = await harness.seed()
    original_lookup = harness.service.async_try_get_actor
    first = True

    async def racing_lookup(name):
        nonlocal first
        if first:
            first = False
            return None
        return await original_lookup(name)

    monkeypatch.setattr(harness.service, "async_try_get_actor", racing_lookup)
    created = []
    selected = await harness.operator._create_actor(
        "vm-1",
        "tenant-a",
        {"vcpus": 2, "memory_mib": 512},
        _PLACEMENT,
        created=created,
    )

    assert selected.handle is existing.handle
    assert created == []
    assert len(harness.service.created) == 1
    assert not harness.service.killed


@pytest.mark.asyncio
async def test_create_actor_placement_failure_preserves_target_and_releases_unstarted_owner(harness):
    harness.service.actor_node_id = "unexpected-node"
    with pytest.raises(OperatorLifecycleError) as exc:
        await harness.operator.submit(_config(), runtime=harness.runtime)

    result = operator_result_from_info(exc.value.sandbox_info)
    assert result.result_unknown
    assert result.target.node_id == _PLACEMENT.node_id
    assert result.target.host_ip == _PLACEMENT.host_ip
    assert len(harness.service.created) == 1
    assert harness.service.created[0].dead
    assert not [call for call in harness.client.calls if call[0] != "status"]


@pytest.mark.asyncio
async def test_snapshot_source_is_verified_by_the_current_actor(harness):
    source = await harness.source()
    harness.client.runtime("source")

    resolved = await harness.operator.resolve_source(source)
    assert resolved == source


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["STARTING", "STOPPED", "FAILED"])
async def test_snapshot_source_must_be_running(harness, state):
    source = await harness.source()
    harness.client.runtime("source", state=state)
    with pytest.raises(SnapdResultUnknown, match="RUNNING"):
        await harness.operator.resolve_source(source)


@pytest.mark.asyncio
async def test_snapshot_source_actor_must_match_durable_placement(harness):
    source = await harness.source()
    source = replace(source, target=OperatorTarget(node_id="wrong-node", host_ip="10.0.0.1"))
    with pytest.raises(SnapdResultUnknown, match="placement"):
        await harness.operator.resolve_source(source)
    assert not harness.client.calls


@pytest.mark.asyncio
async def test_snapshot_capture_is_sent_by_admin_to_source_worker_without_catalog_lookup(harness):
    source = await harness.source()
    request = SnapshotSandboxRequest(snapshot_id="captured")

    result = await harness.operator.capture_snapshot(source, request)

    assert result.state == "LOCAL_READY"
    assert [call[0] for call in harness.client.calls] == ["snapshot"]
    assert {call.method for call in harness.service.calls} <= {"identity", "placement"}
    assert harness.client_urls == ["http://10.0.0.1:22556"]
    assert not harness.service.killed


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [4, 32])
async def test_fork_uses_per_child_resource_actors_and_one_source_batch_rpc(harness, count):
    source = await harness.source()
    result = await harness.operator.fork_snapshot(source, _fork_request(count))

    assert result.state == "LOCAL_READY", result.last_error
    assert result.code is OperatorResultCode.ACCEPTED
    assert [child["sandbox_id"] for child in result.children] == [f"child-{index}" for index in range(count)]
    assert len([call for call in harness.client.calls if call[0] == "fork"]) == 1
    assert not [call for call in harness.client.calls if call[0] == "start"]
    assert len(harness.service.created) == count + 1
    for actor in harness.service.created[1:]:
        assert actor.options["num_cpus"] == 1
        assert actor.options["memory"] == 128 * 1024**2
        assert actor.options["resources"] == {}
        assert actor.options["scheduling_strategy"].node_id == "node-a"
        assert actor.options["scheduling_strategy"].soft is False
        assert not actor.dead
    assert all(child["guest_endpoint"]["host_ip"] == "10.0.0.1" for child in result.children)


@pytest.mark.asyncio
async def test_unknown_fork_batch_keeps_all_child_resources(harness):
    source = await harness.source()
    harness.client.fork_error = SnapdResultUnknown("batch reply was lost")
    result = await harness.operator.fork_snapshot(source, _fork_request(2))
    assert result.code is OperatorResultCode.RESULT_UNKNOWN
    assert len(harness.service.created) == 3
    assert all(not actor.dead for actor in harness.service.created[1:])
    assert not harness.service.killed
    assert len([call for call in harness.client.calls if call[0] == "fork"]) == 1


@pytest.mark.asyncio
async def test_fork_identity_conflict_retains_children_for_observation(harness):
    source = await harness.source()
    harness.client.fork_error = _api_error(SnapdErrorCode.IDENTITY_CONFLICT, "previous batch might exist")

    result = await harness.operator.fork_snapshot(source, _fork_request(2))

    assert result.code is OperatorResultCode.RESULT_UNKNOWN
    assert not harness.service.killed
    assert len(harness.service.registry) == 3
    assert not [call for call in harness.client.calls if call[0] == "stop"]


@pytest.mark.parametrize("failure", ["unknown", "identity_conflict", "invalid_reply"])
async def test_shared_fork_unknown_preserves_failed_admission_and_admitted_placement(harness, failure):
    source = await harness.source()

    async def fail_admission(reference):
        if reference.handle.instance.identity()["sandbox_id"] == "child-0" and reference.method == "placement":
            raise RuntimeError("child placement reply lost")

    harness.service.before_call = fail_admission
    if failure == "unknown":
        harness.client.fork_error = SnapdResultUnknown("batch reply lost")
    elif failure == "identity_conflict":
        harness.client.fork_error = _api_error(SnapdErrorCode.IDENTITY_CONFLICT, "previous batch might exist")
    else:
        harness.client.fork_transform = lambda result: result.model_copy(update={"children": []})

    result = await harness.operator.fork_snapshot(source, _fork_request(2))

    assert result.code is OperatorResultCode.RESULT_UNKNOWN
    assert [child["sandbox_id"] for child in result.children] == ["child-0", "child-1"]
    assert [child["state"] for child in result.children] == ["FAILED", "PENDING"]
    assert result.payload["children"] == list(result.children)
    assert "child placement reply lost" in result.children[0]["last_error"]
    assert result.children[1]["placement"] == {
        "node_id": "node-a",
        "host_ip": "10.0.0.1",
        "host_name": "node-a",
    }
    assert [actor.instance.identity()["sandbox_id"] for actor in harness.service.killed] == ["child-0"]
    assert not harness.service.registry[harness.operator._actor_name("child-1")].dead
    dispatched = [call for call in harness.client.calls if call[0] == "fork"]
    assert [child.sandbox_id for child in dispatched[0][2].children] == ["child-1"]


@pytest.mark.asyncio
async def test_partial_fork_reply_keeps_successful_child_and_releases_failed_child(harness):
    source = await harness.source()

    def partial(result):
        children = [result.children[0], ForkChildResult(sandbox_id="child-1", state="FAILED", error="launch failed")]
        return result.model_copy(update={"children": children})

    harness.client.fork_transform = partial
    result = await harness.operator.fork_snapshot(source, _fork_request(2))

    assert result.code is OperatorResultCode.ACCEPTED
    assert [child["state"] for child in result.children] == ["RUNNING", "FAILED"]
    assert [actor.instance.identity()["sandbox_id"] for actor in harness.service.killed] == ["child-1"]
    assert harness.client.runtimes["child-0"].state == "RUNNING"


@pytest.mark.asyncio
async def test_fork_running_reply_requires_child_actor_confirmation(harness):
    source = await harness.source()

    def missing_child(result):
        harness.client.runtimes.pop("child-1")
        return result

    harness.client.fork_transform = missing_child
    result = await harness.operator.fork_snapshot(source, _fork_request(2))

    assert result.code is OperatorResultCode.ACCEPTED
    assert [child["state"] for child in result.children] == ["RUNNING", "PENDING"]
    assert "guest_endpoint" not in result.children[1]
    assert not harness.service.killed
    assert len(harness.service.registry) == 3


@pytest.mark.asyncio
async def test_failed_fork_confirms_child_stop_before_releasing_resource_actors(harness):
    source = await harness.source()
    harness.client.fork_error = _api_error(SnapdErrorCode.INVALID_ARGUMENT, "invalid fork")

    result = await harness.operator.fork_snapshot(source, _fork_request(2))

    assert result.code is OperatorResultCode.FAILED
    assert all(actor.dead for actor in harness.service.created[1:])
    assert len(harness.service.registry) == 1
    assert [call[1] for call in harness.client.calls if call[0] == "stop"] == ["child-0", "child-1"]


@pytest.mark.asyncio
async def test_fork_reverse_actor_admission_preserves_request_order(harness):
    source = await harness.source()
    completed = []
    seen = set()

    async def delay(reference):
        identity = reference.handle.instance.identity()
        sandbox_id = identity["sandbox_id"]
        if reference.method == "placement" and sandbox_id.startswith("child-") and sandbox_id not in seen:
            seen.add(sandbox_id)
            index = int(sandbox_id.split("-")[1])
            await asyncio.sleep((3 - index) * 0.01)
            completed.append(sandbox_id)

    harness.service.before_call = delay
    result = await harness.operator.fork_snapshot(source, _fork_request())
    assert result.state == "LOCAL_READY", result.last_error
    assert completed == ["child-3", "child-2", "child-1", "child-0"]
    assert [child["sandbox_id"] for child in result.children] == ["child-0", "child-1", "child-2", "child-3"]


@pytest.mark.asyncio
async def test_fork_admission_failure_only_cleans_failed_child_before_rpc(harness):
    source = await harness.source()

    async def fail(reference):
        if reference.handle.instance.identity()["sandbox_id"] == "child-1" and reference.method == "placement":
            raise RuntimeError("placement observation failed")

    harness.service.before_call = fail
    result = await harness.operator.fork_snapshot(source, _fork_request(3))

    assert result.code is OperatorResultCode.ACCEPTED
    assert [child["state"] for child in result.children] == ["RUNNING", "FAILED", "RUNNING"]
    dispatched = [call for call in harness.client.calls if call[0] == "fork"]
    assert len(dispatched) == 1
    assert [child.sandbox_id for child in dispatched[0][2].children] == ["child-0", "child-2"]
    assert [actor.instance.identity()["sandbox_id"] for actor in harness.service.killed] == ["child-1"]
    assert len(harness.service.registry) == 3


@pytest.mark.asyncio
async def test_pending_fork_child_actor_is_killed_without_rolling_back_sibling(harness):
    source = await harness.source()
    attempted_methods = []

    async def no_child_capacity(reference):
        if reference.handle.instance.identity()["sandbox_id"] == "child-1":
            attempted_methods.append(reference.method)
            raise TimeoutError("child actor is waiting for Ray resources")

    harness.service.before_call = no_child_capacity
    result = await harness.operator.fork_snapshot(source, _fork_request(2))

    assert result.code is OperatorResultCode.ACCEPTED
    assert [child["state"] for child in result.children] == ["RUNNING", "FAILED"]
    assert "waiting for Ray resources" in result.children[1]["last_error"]
    (failed_actor,) = harness.service.killed
    assert failed_actor.instance.identity()["sandbox_id"] == "child-1"
    assert failed_actor.options["scheduling_strategy"].node_id == "node-a"
    assert failed_actor.dead
    assert sorted(attempted_methods) == ["identity", "placement"]
    assert harness.operator._actor_name("child-1") not in harness.service.registry
    assert len(harness.service.registry) == 2
    sibling = harness.service.registry[harness.operator._actor_name("child-0")]
    assert not sibling.dead
    dispatched = [call for call in harness.client.calls if call[0] == "fork"]
    assert len(dispatched) == 1
    assert [child.sandbox_id for child in dispatched[0][2].children] == ["child-0"]
    assert all(call[1] != "child-1" for call in harness.client.calls)


@pytest.mark.asyncio
async def test_fork_cancellation_cleans_pre_dispatch_actors(harness):
    source = await harness.source()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block(reference):
        if reference.handle.instance.identity()["sandbox_id"].startswith("child-") and reference.method == "placement":
            entered.set()
            await release.wait()

    harness.service.before_call = block
    task = asyncio.create_task(harness.operator.fork_snapshot(source, _fork_request(2)))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not [call for call in harness.client.calls if call[0] == "fork"]
    assert all(actor.dead for actor in harness.service.created[1:])
    assert len(harness.service.registry) == 1


@pytest.mark.asyncio
async def test_partial_fork_preparation_only_releases_rejected_child(harness):
    source = await harness.source()
    identities = 0

    async def reject(reference):
        nonlocal identities
        if reference.handle.instance.identity()["sandbox_id"] == "child-1" and reference.method == "identity":
            identities += 1
            if identities == 2:
                raise RuntimeError("child preparation reply failed")

    harness.service.before_call = reject
    result = await harness.operator.fork_snapshot(source, _fork_request(2))

    assert result.code is OperatorResultCode.ACCEPTED
    assert [child["state"] for child in result.children] == ["RUNNING", "FAILED"]
    assert [actor.instance.identity()["sandbox_id"] for actor in harness.service.killed] == ["child-1"]
    assert not [call for call in harness.client.calls if call[0] == "stop"]


@pytest.mark.asyncio
async def test_partial_fork_preparation_keeps_already_active_child_resources(harness):
    source = await harness.source()
    identities = 0

    async def reject(reference):
        nonlocal identities
        if reference.handle.instance.identity()["sandbox_id"] == "child-1" and reference.method == "identity":
            identities += 1
            if identities == 2:
                harness.client.runtime("child-1")
                raise RuntimeError("child preparation reply failed")

    harness.service.before_call = reject
    result = await harness.operator.fork_snapshot(source, _fork_request(2))

    assert result.code is OperatorResultCode.ACCEPTED
    assert result.state == "LOCAL_READY"
    assert [child["state"] for child in result.children] == ["RUNNING", "PENDING"]
    assert "cleanup is unknown" in result.children[1]["last_error"]
    child = harness.service.registry[harness.operator._actor_name("child-1")]
    assert not child.dead
    assert not [call for call in harness.client.calls if call[0] == "stop"]
    dispatched = [call for call in harness.client.calls if call[0] == "fork"]
    assert [child.sandbox_id for child in dispatched[0][2].children] == ["child-0"]


@pytest.mark.asyncio
@pytest.mark.parametrize("children", [[], [ChildLaunchPlan(sandbox_id="same")] * 2])
async def test_invalid_internal_fork_batch_is_rejected_before_actors(harness, children):
    request = _fork_request(1).model_copy(update={"children": children})
    result = await harness.operator.fork_snapshot(_snapshot_source(), request)
    assert result.code is OperatorResultCode.IDENTITY_CONFLICT
    assert not harness.service.created
    assert not harness.client.calls


@pytest.mark.asyncio
async def test_invalid_child_resources_do_not_block_other_children(harness):
    source = await harness.source()
    request = _fork_request(2)
    request.children[0].resource_spec["memory_mib"] = 0
    result = await harness.operator.fork_snapshot(source, request)
    assert result.code is OperatorResultCode.ACCEPTED
    assert [child["state"] for child in result.children] == ["FAILED", "RUNNING"]
    assert "positive" in result.children[0]["last_error"]
    assert len(harness.service.created) == 2
    assert [child.sandbox_id for child in harness.client.calls[0][2].children] == ["child-1"]


async def test_no_admitted_fork_children_fail_without_dispatch(harness):
    source = await harness.source()
    request = _fork_request(2)
    for child in request.children:
        child.resource_spec["memory_mib"] = 0

    result = await harness.operator.fork_snapshot(source, request)

    assert result.code is OperatorResultCode.FAILED
    assert [child["state"] for child in result.children] == ["FAILED", "FAILED"]
    assert len(harness.service.created) == 1
    assert not harness.client.calls


async def test_first_child_attach_failure_does_not_hide_later_successes(harness):
    source = await harness.source()

    async def fail(sandbox_id):
        if sandbox_id == "child-0":
            raise RuntimeError("child attach reply lost")

    harness.client.before_status = fail
    result = await harness.operator.fork_snapshot(source, _fork_request(3))

    assert result.code is OperatorResultCode.ACCEPTED
    assert [child["state"] for child in result.children] == ["PENDING", "RUNNING", "RUNNING"]
    assert "guest_endpoint" not in result.children[0]
    assert not harness.service.killed
    assert all(runtime.state == "RUNNING" for runtime in harness.client.runtimes.values())


async def test_failed_child_cleanup_failure_does_not_hide_later_successes(harness):
    source = await harness.source()

    def partial(result):
        return result.model_copy(
            update={
                "children": [
                    ForkChildResult(sandbox_id="child-0", state="FAILED", error="launch failed"),
                    *result.children[1:],
                ]
            }
        )

    harness.client.fork_transform = partial
    harness.client.stop_error = SnapdResultUnknown("stop reply lost")
    result = await harness.operator.fork_snapshot(source, _fork_request(3))

    assert result.code is OperatorResultCode.ACCEPTED
    assert [child["state"] for child in result.children] == ["PENDING", "RUNNING", "RUNNING"]
    assert "stop reply lost" in result.children[0]["last_error"]
    assert not harness.service.killed


async def test_daemon_pending_child_keeps_resource_owner_without_stopping_successful_siblings(harness):
    source = await harness.source()

    def partial(result):
        pending = ForkChildResult(sandbox_id="child-0", state="FAILED", error="child launch reply lost")
        return result.model_copy(
            update={
                "children": [
                    pending.model_copy(update={"state": "PENDING"}),
                    *result.children[1:],
                ]
            }
        )

    harness.client.fork_transform = partial
    result = await harness.operator.fork_snapshot(source, _fork_request(3))

    assert result.code is OperatorResultCode.ACCEPTED
    assert [child["state"] for child in result.children] == ["PENDING", "RUNNING", "RUNNING"]
    assert result.children[0]["last_error"] == "child launch reply lost"
    assert "guest_endpoint" not in result.children[0]
    assert not harness.service.killed
    assert not [call for call in harness.client.calls if call[0] == "stop"]


async def test_failed_batch_cleanup_keeps_unknown_child_without_blocking_other_cleanup(harness):
    source = await harness.source()
    harness.client.fork_error = _api_error(SnapdErrorCode.INVALID_ARGUMENT, "invalid fork")

    async def fail(sandbox_id):
        if sandbox_id == "child-0":
            raise RuntimeError("stop reply lost")

    harness.client.before_stop = fail
    result = await harness.operator.fork_snapshot(source, _fork_request(3))

    assert result.code is OperatorResultCode.FAILED
    assert [child["state"] for child in result.children] == ["PENDING", "FAILED", "FAILED"]
    assert [actor.instance.identity()["sandbox_id"] for actor in harness.service.killed] == ["child-1", "child-2"]
    assert not harness.service.registry[harness.operator._actor_name("child-0")].dead


def test_duck_typed_snapshot_backend_does_not_pass_nominal_capability_check() -> None:
    fake = type("DuckSnapshot", (), {name: MagicMock() for name in AbstractSnapshotOperator.__abstractmethods__})()
    assert not isinstance(fake, AbstractSnapshotOperator)


def test_incomplete_snapshot_backend_cannot_be_instantiated() -> None:
    class Incomplete(AbstractOperator, AbstractSnapshotOperator):
        pass

    with pytest.raises(TypeError, match="abstract"):
        Incomplete()


@pytest.mark.asyncio
async def test_base_image_replay_uses_catalog_owner_without_discovery() -> None:
    absent = _SnapshotOwnerClient(owns_snapshot=False)
    owner = _SnapshotOwnerClient(owns_snapshot=True)
    pool = _SnapshotClientPool(absent, owner)
    operator = SnapdOperator(MagicMock(), _client_pool=pool)  # type: ignore[arg-type]

    result = await operator.create_base_image(_base_request("base-global"), source_worker_ip="10.0.0.2")

    assert result.code is OperatorResultCode.ACCEPTED
    assert result.payload["source_worker_ip"] == "10.0.0.2"
    assert pool.calls == ["10.0.0.2"]
    assert absent.calls == []
    assert owner.calls == ["create:base-global"]


@pytest.mark.asyncio
async def test_base_image_resolution_is_sent_by_admin_after_ray_selects_worker(monkeypatch) -> None:
    owner = _SnapshotOwnerClient(owns_snapshot=False)
    pool = _SnapshotClientPool(owner)
    service = MagicMock()
    service.get_ray_rwlock.return_value = _Lock()
    service.async_ray_get = AsyncMock(return_value="10.0.0.1")
    resolver = MagicMock()
    monkeypatch.setattr("rock.sandbox.operator.snapd_actor.select_snapd_worker", resolver)
    operator = SnapdOperator(service, _client_pool=pool)

    result = await operator.resolve_base_image(
        ResolveBaseImageRequest(oci_ref="registry.example/base:latest", platform="linux/amd64")
    )

    assert result.snapshot_id == "sha256:" + "b" * 64
    assert result.payload["source_worker_ip"] == "10.0.0.1"
    assert result.payload["oci_digest"] == "sha256:" + "a" * 64
    resolver.remote.assert_called_once_with()
    service.async_ray_get.assert_awaited_once_with(
        resolver.remote.return_value,
        timeout=35,
    )
    assert pool.calls == ["10.0.0.1"]
    assert owner.calls == ["resolve:registry.example/base:latest"]


@pytest.mark.asyncio
async def test_base_image_resolution_does_not_replay_a_stale_mutable_tag_result(monkeypatch) -> None:
    service = MagicMock()
    service.get_ray_rwlock.return_value = _Lock()
    service.async_ray_get = AsyncMock(return_value="10.0.0.1")
    resolver = MagicMock()
    monkeypatch.setattr("rock.sandbox.operator.snapd_actor.select_snapd_worker", resolver)
    owner = _SnapshotOwnerClient(owns_snapshot=False)
    operator = SnapdOperator(service, _client_pool=_SnapshotClientPool(owner))
    request = ResolveBaseImageRequest(
        oci_ref="registry.example/base:mutable",
        platform="linux/amd64",
    )

    await operator.resolve_base_image(request)
    await operator.resolve_base_image(request)

    calls = resolver.remote.call_args_list
    assert len(calls) == 2
    assert all(not call.args and not call.kwargs for call in calls)
    assert owner.calls == ["resolve:registry.example/base:mutable"] * 2


@pytest.mark.asyncio
async def test_base_status_and_finalize_target_only_catalog_owner_without_extra_get() -> None:
    absent = _SnapshotOwnerClient(owns_snapshot=False)
    owner = _SnapshotOwnerClient(owns_snapshot=True)
    pool = _SnapshotClientPool(absent, owner)
    operator = SnapdOperator(MagicMock(), _client_pool=pool)  # type: ignore[arg-type]

    status = await operator.get_snapshot_status("base-global", source_worker_ip="10.0.0.2")
    finalized = await operator.finalize_published_snapshot(
        "base-global",
        FinalizePublishedSnapshotRequest(spec_checksum="sha256:" + "a" * 64),
        source_worker_ip="10.0.0.2",
    )

    assert status.state == "REMOTE_READY"
    assert finalized.state == "REMOTE_READY"
    assert pool.calls == ["10.0.0.2", "10.0.0.2"]
    assert absent.calls == []
    assert owner.calls == ["status:base-global", "finalize:base-global"]


@pytest.mark.asyncio
async def test_base_image_catalog_owner_does_not_probe_other_claimants() -> None:
    first = _SnapshotOwnerClient(owns_snapshot=True)
    second = _SnapshotOwnerClient(owns_snapshot=True)
    pool = _SnapshotClientPool(first, second)
    operator = SnapdOperator(MagicMock(), _client_pool=pool)  # type: ignore[arg-type]

    result = await operator.create_base_image(_base_request("base-conflict"), source_worker_ip="10.0.0.1")

    assert result.code is OperatorResultCode.ACCEPTED
    assert first.calls == ["create:base-conflict"]
    assert second.calls == []


@pytest.mark.asyncio
async def test_unrelated_worker_failure_does_not_block_catalog_owner() -> None:
    owner = _SnapshotOwnerClient(owns_snapshot=True)
    uncertain = _SnapshotOwnerClient(
        owns_snapshot=False,
        observation_failure=SnapdResultUnknown("worker reply was lost"),
    )
    pool = _SnapshotClientPool(owner, uncertain)
    operator = SnapdOperator(MagicMock(), _client_pool=pool)  # type: ignore[arg-type]

    result = await operator.create_base_image(_base_request("base-uncertain"), source_worker_ip="10.0.0.1")

    assert result.code is OperatorResultCode.ACCEPTED
    assert owner.calls == ["create:base-uncertain"]
    assert uncertain.calls == []


@pytest.mark.parametrize("operation", ["get_status", "stop", "delete"])
@pytest.mark.parametrize("node_id,host_ip", [("replacement-node", "10.0.0.1"), ("node-a", "10.0.0.2")])
async def test_missing_actor_uses_persisted_worker_without_scheduling_on_replacement(harness, operation, node_id, host_ip):
    harness.service.actor_node_id = node_id
    harness.service.actor_host_ip = host_ip

    if operation == "delete":
        await harness.operator.delete(_config(), host_ip="10.0.0.1", runtime=harness.runtime)
    else:
        await getattr(harness.operator, operation)("vm-1", runtime=harness.runtime)

    assert harness.client_urls == ["http://10.0.0.1:22556"]
    assert not harness.service.created
    assert not harness.service.killed


async def test_cancelled_admin_start_keeps_resource_owner(harness):
    harness.client.start_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await harness.operator.submit(_config(), runtime=harness.runtime)
    assert not harness.service.created[0].dead
    assert not harness.service.killed


async def test_start_identity_conflict_keeps_resource_owner(harness):
    harness.client.start_error = _api_error(SnapdErrorCode.IDENTITY_CONFLICT, "VM may already exist")
    with pytest.raises(OperatorLifecycleError) as caught:
        await harness.operator.submit(_config(), runtime=harness.runtime)
    assert operator_result_from_info(caught.value.sandbox_info).result_unknown
    assert not harness.service.killed


async def test_status_reply_for_another_sandbox_does_not_release_resources(harness):
    actor = await harness.seed()
    harness.client.runtimes["vm-1"] = harness.client.runtime("another-sandbox", "STOPPED")
    with pytest.raises(OperatorLifecycleError, match="another sandbox"):
        await harness.operator.get_status("vm-1")
    assert not actor.handle.dead


async def test_admin_start_omits_same_worker_p2p_source_and_hashes_snapshot_spec(harness):
    import hashlib
    import json

    harness.runtime = replace(harness.runtime, source_worker_ip="10.0.0.1")
    await harness.operator.submit(_config(), runtime=harness.runtime)
    (start,) = [call for call in harness.client.calls if call[0] == "start"]
    assert start[2].p2p_source_worker_ip is None
    canonical = json.dumps(harness.runtime.snapshot_spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert start[2].spec_checksum == "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


@pytest.mark.parametrize("operation", ["create", "status", "finalize"])
async def test_missing_snapshot_owner_returns_unknown_without_ray_discovery(harness, operation):
    if operation == "create":
        result = await harness.operator.create_base_image(_base_request("base"), source_worker_ip="")
    elif operation == "status":
        result = await harness.operator.get_snapshot_status("base", source_worker_ip="")
    else:
        result = await harness.operator.finalize_published_snapshot(
            "base",
            FinalizePublishedSnapshotRequest(spec_checksum="sha256:" + "a" * 64),
            source_worker_ip="",
        )
    assert result.code is OperatorResultCode.RESULT_UNKNOWN
    assert harness.client.calls == []
    assert harness.service.created == []
