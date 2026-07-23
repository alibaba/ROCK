import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import rock.sandbox.service.commit_worker as commit_worker
from rock import env_vars
from rock.actions import CommandResponse, CommitErrorCode, CommitPhase, CommitStatusResponse
from rock.admin.entrypoints.sandbox_proxy_api import sandbox_proxy_router, set_sandbox_proxy_service
from rock.admin.proto.request import CommitRequest
from rock.deployments.constants import Port
from rock.sandbox.service.commit_worker import (
    DISPATCH_TIMEOUT_SECONDS,
    WORKER_STATUS_DIR,
    CommitShellBuilder,
    CommitWorkerExecutor,
    WorkerCommitState,
)
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sdk.common.exceptions import WorkerCommitError
from rock.sdk.sandbox.client import Sandbox
from rock.utils import HttpUtils


class _FakeMetaStore:
    def __init__(self, sandbox: dict | None):
        self.sandbox = sandbox
        self.calls: list[tuple[str, bool]] = []

    async def get(self, sandbox_id: str, check_db: bool = False) -> dict | None:
        self.calls.append((sandbox_id, check_db))
        return self.sandbox


class _ScriptedWorkerExecutor:
    def __init__(
        self,
        *,
        starts: list[WorkerCommitState | WorkerCommitError] | None = None,
        statuses: list[WorkerCommitState | WorkerCommitError] | None = None,
    ):
        self.starts = [] if starts is None else starts
        self.statuses = [] if statuses is None else statuses

    async def start(self, _host_ip: str, _request: CommitRequest) -> WorkerCommitState:
        return self._next(self.starts)

    async def get_status(self, _host_ip: str, _sandbox_id: str) -> WorkerCommitState:
        return self._next(self.statuses)

    @staticmethod
    def _next(script: list[WorkerCommitState | WorkerCommitError]) -> WorkerCommitState:
        result = script.pop(0)
        if isinstance(result, WorkerCommitError):
            raise result
        return result


class _CapturingRuntime:
    def __init__(self):
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return CommandResponse()


def _state(phase: CommitPhase, *, error_code: CommitErrorCode | None = None) -> WorkerCommitState:
    return WorkerCommitState(
        phase=phase,
        image_tag="registry.example/app:v1",
        pid=42,
        pid_start_ticks=7,
        run_started_at=1,
        started_at="2026-01-01T00:00:00Z",
        error_code=error_code,
    )


def _service(meta_store: _FakeMetaStore, worker: _ScriptedWorkerExecutor) -> SandboxProxyService:
    service = object.__new__(SandboxProxyService)
    service._meta_store = meta_store
    service._commit_worker_executor = worker
    return service


def _app(service: SandboxProxyService) -> FastAPI:
    app = FastAPI()
    set_sandbox_proxy_service(service)
    app.include_router(sandbox_proxy_router)
    return app


async def _client(service: SandboxProxyService):
    return AsyncClient(transport=ASGITransport(app=_app(service)), base_url="http://test")


def _commit_payload(sandbox_id: str = "sandbox-1") -> dict[str, str]:
    return {
        "sandbox_id": sandbox_id,
        "image_tag": "registry.example/app:v1",
        "username": "user",
        "password": "secret",
    }


@pytest.mark.asyncio
async def test_commit_post_running_then_get_succeeded(monkeypatch: pytest.MonkeyPatch):
    sandbox_id = "sandbox:one"
    meta_store = _FakeMetaStore({"host_ip": "10.0.0.8"})
    worker = _ScriptedWorkerExecutor(
        starts=[_state(CommitPhase.RUNNING)],
        statuses=[_state(CommitPhase.SUCCEEDED)],
    )

    async with await _client(_service(meta_store, worker)) as client:
        post = await client.post("/commit", json=_commit_payload(sandbox_id))
        get = await client.get(f"/commit/{sandbox_id}")

    assert post.json()["result"]["phase"] == "RUNNING"
    assert get.json()["result"]["phase"] == "SUCCEEDED"
    assert meta_store.calls == [(sandbox_id, True), (sandbox_id, True)]

    requested_urls = []

    async def get_commit_status(url: str, _headers: dict) -> dict:
        requested_urls.append(url)
        return {
            "status": "Success",
            "result": {
                "sandbox_id": "sdk?#%",
                "image_tag": "registry.example/app:v1",
                "phase": "RUNNING",
                "started_at": "2026-01-01T00:00:00Z",
            },
        }

    sdk_sandbox = object.__new__(Sandbox)
    sdk_sandbox._sandbox_id = "sdk?#%"
    sdk_sandbox._url = "http://admin"
    sdk_sandbox._build_headers = lambda: {}
    monkeypatch.setattr(HttpUtils, "get", get_commit_status)
    await sdk_sandbox.get_commit_status()
    assert requested_urls == ["http://admin/commit/sdk%3F%23%25"]


@pytest.mark.asyncio
async def test_commit_rejects_protocol_delimiters_and_maps_worker_errors():
    assert WORKER_STATUS_DIR == f"{env_vars.ROCK_SERVICE_STATUS_DIR}/commit"
    status_paths = CommitShellBuilder(WORKER_STATUS_DIR)
    for sandbox_id in ("contains/slash", "contains\x00nul"):
        with pytest.raises(ValueError):
            status_paths.build_query(sandbox_id)
    shell_request = CommitRequest(**_commit_payload())
    start_script = status_paths.build_start(shell_request, started_at="2026-01-01T00:00:00Z", run_started_at=1)
    query_script = status_paths.build_query(shell_request.sandbox_id)
    assert "current_pid_state=$(awk '{print $3}' \"/proc/$old_pid/stat\"" in start_script
    assert (
        '[ "$current_pid_state" != Z ] && [ -n "$current_pid_start_ticks" ] '
        '&& [ "$current_pid_start_ticks" = "$old_pid_start_ticks" ]' in start_script
    )
    assert "current_pid_state=$(awk '{print $3}' \"/proc/$pid/stat\"" in query_script
    assert (
        '[ "$current_pid_state" != Z ] && [ -n "$current_pid_start_ticks" ] '
        '&& [ "$current_pid_start_ticks" = "$pid_start_ticks" ]' in query_script
    )

    meta_store = _FakeMetaStore({"host_ip": "10.0.0.8"})
    worker = _ScriptedWorkerExecutor(
        starts=[WorkerCommitError(CommitErrorCode.COMMIT_CONFLICT, "already running")],
        statuses=[WorkerCommitError(CommitErrorCode.WORKER_UNREACHABLE, "offline")],
    )

    async with await _client(_service(meta_store, worker)) as client:
        for separator in ("\u000b", "\u2028"):
            for image_tag in (
                f"{separator}registry.example/app:v1",
                f"registry.example/app:v1{separator}",
                f"registry.example/app:v1{separator}phase=SUCCEEDED",
            ):
                invalid = await client.post(
                    "/commit",
                    json=_commit_payload() | {"image_tag": image_tag},
                )
                assert invalid.status_code == 422
        conflict = await client.post("/commit", json=_commit_payload())
        unreachable = await client.get("/commit/sandbox-1")

    assert conflict.json()["result"]["code"] == 4000
    assert unreachable.json()["result"]["code"] == 5000


@pytest.mark.asyncio
async def test_commit_uses_database_metadata_and_returns_not_found_and_process_lost(monkeypatch: pytest.MonkeyPatch):
    runtime = _CapturingRuntime()
    runtime_arguments = []

    def capturing_runtime(*, host: str, port: int):
        runtime_arguments.append((host, port))
        return runtime

    monkeypatch.setattr(commit_worker, "RemoteSandboxRuntime", capturing_runtime)
    executor = CommitWorkerExecutor()
    await executor._execute("10.0.0.8", "sandbox-1", "echo committed")
    assert runtime_arguments == [("10.0.0.8", Port.PROXY.value)]
    transport_request = runtime.requests[0]
    assert transport_request.command == ["bash", "-c", "echo committed"]
    assert transport_request.shell is False
    assert transport_request.timeout == DISPATCH_TIMEOUT_SECONDS

    running_response = {
        "status": "Success",
        "result": {
            "sandbox_id": "sandbox-1",
            "image_tag": "registry.example/app:v1",
            "phase": "RUNNING",
            "started_at": "2026-01-01T00:00:00Z",
        },
    }
    succeeded_response = {
        "status": "Success",
        "result": {
            "sandbox_id": "sandbox-1",
            "image_tag": "registry.example/app:v1",
            "phase": "SUCCEEDED",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:05Z",
            "exit_code": 0,
        },
    }
    post_responses = [running_response, running_response, running_response]
    get_responses = [succeeded_response, succeeded_response]
    wait_timeouts = []
    sleep_intervals = []

    async def post_response(_url: str, _headers: dict, _data: dict) -> dict:
        return post_responses.pop(0)

    async def get_response(_url: str, _headers: dict) -> dict:
        return get_responses.pop(0)

    async def wait_for(awaitable, timeout: float):
        wait_timeouts.append(timeout)
        return await awaitable

    async def sleep(interval: float):
        sleep_intervals.append(interval)

    sdk_sandbox = object.__new__(Sandbox)
    sdk_sandbox._sandbox_id = "sandbox-1"
    sdk_sandbox._url = "http://admin"
    sdk_sandbox._build_headers = lambda: {}
    monkeypatch.setattr(HttpUtils, "post", post_response)
    monkeypatch.setattr(HttpUtils, "get", get_response)
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    default_commit = await sdk_sandbox.commit("registry.example/app:v1", "user", "secret")
    custom_commit = await sdk_sandbox.commit("registry.example/app:v1", "user", "secret", timeout=7, interval=0.25)
    assert (
        default_commit
        == custom_commit
        == CommitStatusResponse(
            sandbox_id="sandbox-1",
            image_tag="registry.example/app:v1",
            phase=CommitPhase.SUCCEEDED,
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:05Z",
            exit_code=0,
        )
    )
    assert wait_timeouts == [180, 7]
    assert sleep_intervals == [2, 0.25]

    async_commit = await sdk_sandbox.commit_async("registry.example/app:v1", "user", "secret")
    assert async_commit == CommitStatusResponse(
        sandbox_id="sandbox-1",
        image_tag="registry.example/app:v1",
        phase=CommitPhase.RUNNING,
        started_at="2026-01-01T00:00:00Z",
    )

    missing_meta_store = _FakeMetaStore(None)
    process_lost = _state(CommitPhase.FAILED, error_code=CommitErrorCode.PROCESS_LOST)
    worker = _ScriptedWorkerExecutor(statuses=[process_lost])
    present_meta_store = _FakeMetaStore({"host_ip": "10.0.0.8"})

    async with await _client(_service(missing_meta_store, worker)) as client:
        missing = await client.post("/commit", json=_commit_payload("missing"))
    async with await _client(_service(present_meta_store, worker)) as client:
        lost = await client.get("/commit/sandbox-1")

    assert missing_meta_store.calls == [("missing", True)]
    assert missing.json()["result"]["code"] == 4000
    assert missing.json()["result"]["failure_reason"] == "SANDBOX_NOT_FOUND: missing"
    assert present_meta_store.calls == [("sandbox-1", True)]
    assert lost.json()["result"]["phase"] == "FAILED"
    assert lost.json()["result"]["error_code"] == "PROCESS_LOST"
