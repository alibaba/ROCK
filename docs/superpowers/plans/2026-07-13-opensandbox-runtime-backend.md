# OpenSandbox Runtime Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route ROCK command, file, upload, and single-sandbox status operations to OpenSandbox without installing or contacting Rocklet in OpenSandbox sandboxes.

**Architecture:** Extract the current Rocklet HTTP behavior behind a narrow `SandboxRuntimeBackend` protocol, then make `SandboxProxyService` choose a backend from fail-closed sandbox metadata. Extend the existing lazy `OpenSandboxClient` facade with runtime primitives and implement ROCK-compatible command, file, upload, and status behavior without exposing SDK types.

**Tech Stack:** Python 3.10+, FastAPI `UploadFile`, Pydantic v2, httpx, `opensandbox==0.1.13`, pytest/pytest-asyncio, Ruff.

## Global Constraints

- `backend="opensandbox"` never falls back to Rocklet and never requires `host_ip`, port mapping, or port 22555.
- Sandbox metadata is the primary routing source; missing, unknown, or operator-conflicting metadata fails closed except for missing metadata under a legacy Rocklet-backed operator.
- Runtime operations require `State.RUNNING`; PENDING fails immediately without proxy-side polling.
- The first PR supports execute, read, write, streaming upload, `get_status`, and `is_alive`; OpenSandbox sessions, portforward, scheduler changes, and remote-command cancellation are out of scope.
- OpenSandbox network routing strictly follows `use_server_proxy`; no automatic network fallback is allowed.
- String commands use OpenSandbox shell semantics and warn when `shell=False`; list commands use `shlex.join()`.
- Upload passes a binary `IOBase` through to the SDK and must not call `read()` on the whole file.
- New files use mode `0644`; overwrites preserve the existing mode.
- Logs must not include command text, file content, environment values, API keys, or endpoint headers.
- Real OpenSandbox E2E is required for an unqualified success claim; otherwise the PR must disclose that it is incomplete.

---

## File Structure

- Create `rock/sandbox/service/backends/base.py`: backend protocol and backend-name constants.
- Create `rock/sandbox/service/backends/rocklet.py`: current Rocklet HTTP transport and response mapping.
- Create `rock/sandbox/service/backends/opensandbox.py`: ROCK model mapping and compatibility policy for OpenSandbox.
- Create `rock/sandbox/service/backends/__init__.py`: focused exports.
- Modify `rock/sandbox/service/sandbox_proxy_service.py`: registry assembly, fail-closed routing, capability guards, and backend-aware status.
- Modify `rock/sandbox/operator/opensandbox/client.py`: shared transport ownership and SDK runtime primitives.
- Create `tests/unit/sandbox/service/backends/test_rocklet_backend.py`: Rocklet characterization coverage.
- Create `tests/unit/sandbox/service/backends/test_opensandbox_backend.py`: command/file/upload mapping coverage.
- Create `tests/unit/sandbox/service/test_runtime_backend_routing.py`: routing, capability, state, and status coverage.
- Modify `tests/unit/sandbox/operator/opensandbox/test_opensandbox_client.py`: SDK facade and resource lifecycle coverage.
- Modify `docs/plans/opensandbox-operator-plan.md`: mark the delivered Phase 2 subset and deferred capabilities accurately.

### Task 1: Characterize and extract the Rocklet backend

**Files:**
- Create: `rock/sandbox/service/backends/base.py`
- Create: `rock/sandbox/service/backends/rocklet.py`
- Create: `rock/sandbox/service/backends/__init__.py`
- Create: `tests/unit/sandbox/service/backends/test_rocklet_backend.py`
- Modify: `rock/sandbox/service/sandbox_proxy_service.py:55-80,644-688`

**Interfaces:**
- Produces: `SandboxRuntimeBackend` protocol with async `execute`, `read_file`, `write_file`, and `upload` methods.
- Produces: `RockletBackend(rpc_client)` preserving `_send_request` behavior.
- Consumes: existing `ServiceStatus`, `Port.PROXY`, trace context, ROCK request/response models.

- [ ] **Step 1: Write failing Rocklet characterization tests**

Create tests that instantiate `RockletBackend` with an `AsyncMock` client and assert URL, headers, JSON/form/multipart fields, 511 mapping, 504 mapping, and network failure behavior. Use a RUNNING status fixture containing `host_ip` and `port_mapping`.

```python
@pytest.mark.asyncio
async def test_execute_preserves_rocklet_request_contract():
    rpc = AsyncMock()
    rpc.request.return_value = _response(200, {"stdout": "ok\n", "stderr": "", "exit_code": 0})
    backend = RockletBackend(rpc)
    status = {"host_ip": "10.0.0.8", "port_mapping": {str(Port.PROXY): 30123}}

    result = await backend.execute("sbx-1", status, SandboxCommand(command=["echo", "ok"], sandbox_id="sbx-1"))

    assert result.exit_code == 0
    rpc.request.assert_awaited_once_with(
        method="POST",
        url="http://10.0.0.8:30123/execute",
        headers={"sandbox_id": "sbx-1", EAGLE_EYE_TRACE_ID: ANY},
        json=ANY,
        data=None,
        files=None,
    )
```

- [ ] **Step 2: Run characterization tests and verify RED**

Run: `uv run pytest tests/unit/sandbox/service/backends/test_rocklet_backend.py -v`

Expected: collection fails because `rock.sandbox.service.backends.rocklet` does not exist.

- [ ] **Step 3: Define the narrow protocol and Rocklet implementation**

```python
class SandboxRuntimeBackend(Protocol):
    async def execute(self, sandbox_id: str, info: dict, command: SandboxCommand) -> CommandResponse: ...
    async def read_file(self, sandbox_id: str, info: dict, request: SandboxReadFileRequest) -> ReadFileResponse: ...
    async def write_file(self, sandbox_id: str, info: dict, request: SandboxWriteFileRequest) -> WriteFileResponse: ...
    async def upload(self, sandbox_id: str, info: dict, file: UploadFile, target_path: str) -> UploadResponse: ...
```

Move URL/header/request/error code from `_send_request` into `RockletBackend` without changing behavior. Keep only backend-neutral orchestration in `SandboxProxyService`.

- [ ] **Step 4: Run focused and existing proxy tests**

Run: `uv run pytest tests/unit/sandbox/service/backends/test_rocklet_backend.py tests/unit/sandbox/test_proxy_enhancements.py -v`

Expected: PASS with no request-contract changes.

- [ ] **Step 5: Commit the extraction**

```bash
git add rock/sandbox/service/backends tests/unit/sandbox/service/backends/test_rocklet_backend.py rock/sandbox/service/sandbox_proxy_service.py
git commit -m "refactor(proxy): extract Rocklet runtime backend"
```

### Task 2: Add fail-closed backend routing and capability guards

**Files:**
- Create: `tests/unit/sandbox/service/test_runtime_backend_routing.py`
- Modify: `rock/sandbox/service/sandbox_proxy_service.py:55-205,312-595,638-688`
- Modify: `rock/sandbox/service/backends/base.py`

**Interfaces:**
- Consumes: backend registry `Mapping[str, SandboxRuntimeBackend]` and `rock_config.runtime.operator_type`.
- Produces: `_get_runtime_info(sandbox_id: str) -> dict` and `_resolve_backend(info: dict) -> SandboxRuntimeBackend`.
- Produces: explicit OpenSandbox capability errors for session and portforward methods.

- [ ] **Step 1: Write routing matrix tests**

Parameterize these cases: explicit Rocklet under `ray`; missing backend under `ray`; explicit OpenSandbox under `opensandbox`; missing backend plus `opensandbox_id` under `opensandbox`; explicit metadata/operator conflict; unknown backend; PENDING; STOPPED. Inject fake backends and assert only the expected fake is called.

```python
@pytest.mark.asyncio
async def test_opensandbox_missing_backend_fails_without_rocklet_call(service, meta_store, rocklet):
    service._rock_config.runtime.operator_type = "opensandbox"
    meta_store.get.return_value = {
        "sandbox_id": "sbx-1",
        "state": State.RUNNING,
        "extended_params": {"opensandbox_id": "osb-1"},
    }

    with pytest.raises(BadRequestRockError, match="backend"):
        await service.execute(SandboxCommand(command="pwd", sandbox_id="sbx-1"))

    rocklet.execute.assert_not_awaited()
```

- [ ] **Step 2: Run routing tests and verify RED**

Run: `uv run pytest tests/unit/sandbox/service/test_runtime_backend_routing.py -v`

Expected: FAIL because routing still requires `host_ip` and does not validate metadata/operator conflicts.

- [ ] **Step 3: Implement backend-neutral metadata retrieval and routing**

Implement `_get_runtime_info` to load metadata, require RUNNING, and avoid transport fields. Implement `_resolve_backend` with constants and the exact fail-closed matrix. Wire execute/read/write/upload through the selected backend.

- [ ] **Step 4: Add explicit capability guards**

Resolve backend before session/portforward dispatch. If OpenSandbox, raise `BadRequestRockError("OpenSandbox backend does not support <capability> in this release")`; otherwise preserve current Rocklet behavior.

- [ ] **Step 5: Run routing and proxy regression tests**

Run: `uv run pytest tests/unit/sandbox/service/test_runtime_backend_routing.py tests/unit/sandbox/test_proxy_enhancements.py -v`

Expected: PASS; tests assert zero Rocklet calls for every OpenSandbox failure path.

- [ ] **Step 6: Commit routing**

```bash
git add rock/sandbox/service/sandbox_proxy_service.py rock/sandbox/service/backends/base.py tests/unit/sandbox/service/test_runtime_backend_routing.py
git commit -m "feat(proxy): add fail-closed runtime backend routing"
```

### Task 3: Extend the OpenSandbox client facade and resource lifecycle

**Files:**
- Modify: `rock/sandbox/operator/opensandbox/client.py`
- Modify: `tests/unit/sandbox/operator/opensandbox/test_opensandbox_client.py`

**Interfaces:**
- Produces: `execute(opensandbox_id, command, opts)`, `read_bytes(opensandbox_id, path)`, `get_file_info(opensandbox_id, path)`, and `write_file(opensandbox_id, path, data, mode)` primitives.
- Produces: `OpenSandboxClient.aclose()` and a shared transport owned exactly once.
- Consumes: SDK `Sandbox.connect`, `RunCommandOpts`, binary `IOBase`, and `ConnectionConfig`.

- [ ] **Step 1: Write failing facade tests**

Use injected fake Sandbox and ConnectionConfig classes. Assert `request_timeout=timedelta(seconds=config.default_timeout)`, `use_server_proxy` preservation, `skip_health_check=True`, temporary-handle close on success/error/cancellation, lazy import behavior, and one shared transport close.

```python
@pytest.mark.asyncio
async def test_runtime_operation_closes_temporary_handle(fake_sdk):
    client = OpenSandboxClient(_config(), sandbox_cls=fake_sdk.sandbox_cls, connection_config_cls=fake_sdk.config_cls)

    await client.read_bytes("osb-1", "/tmp/a")

    fake_sdk.sandbox_cls.connect.assert_awaited_once()
    fake_sdk.handle.close.assert_awaited_once()
```

- [ ] **Step 2: Run client tests and verify RED**

Run: `uv run pytest tests/unit/sandbox/operator/opensandbox/test_opensandbox_client.py -v`

Expected: FAIL because runtime primitives, timeout mapping, and `aclose` do not exist.

- [ ] **Step 3: Implement connection configuration and runtime primitives**

Pass a client-owned `httpx.AsyncHTTPTransport` and positive `request_timeout`. Use one internal async context manager to connect with `skip_health_check=True` and close the handle in `finally`. Translate raw SDK exceptions to ROCK exceptions without exposing the API key or headers.

- [ ] **Step 4: Add positive timeout validation in the client**

Reject `default_timeout <= 0` in `OpenSandboxClient.__init__` with an explicit `ValueError`. Add tests for zero and negative values.

- [ ] **Step 5: Run OpenSandbox operator/client regression tests**

Run: `uv run pytest tests/unit/sandbox/operator/opensandbox -v`

Expected: PASS, including all PR #1203 lifecycle tests.

- [ ] **Step 6: Commit the client facade**

```bash
git add rock/sandbox/operator/opensandbox/client.py tests/unit/sandbox/operator/opensandbox/test_opensandbox_client.py
git commit -m "feat(opensandbox): add runtime client operations"
```

### Task 4: Implement OpenSandbox command execution

**Files:**
- Create: `rock/sandbox/service/backends/opensandbox.py`
- Modify: `rock/sandbox/service/backends/__init__.py`
- Create: `tests/unit/sandbox/service/backends/test_opensandbox_backend.py`
- Modify: `rock/sandbox/service/sandbox_proxy_service.py:55-80`

**Interfaces:**
- Consumes: Task 3 `OpenSandboxClient.execute` and Task 2 backend registry.
- Produces: `OpenSandboxBackend.execute(...) -> CommandResponse`.

- [ ] **Step 1: Write failing command mapping tests**

Cover list `shlex.join`, string passthrough, `shell=False` warning without command text, cwd/env/timeout mapping, stdout/stderr accumulation, `check=False`, `check=True`, `error_msg`, timeout translation, and missing `opensandbox_id`.

```python
@pytest.mark.asyncio
async def test_list_command_preserves_argument_boundaries(client):
    client.execute.return_value = _execution(stdout="ok", stderr="", exit_code=0)
    backend = OpenSandboxBackend(client)

    await backend.execute("sbx-1", _info(), SandboxCommand(command=["echo", "hello world"], sandbox_id="sbx-1"))

    assert client.execute.await_args.args[1] == "echo 'hello world'"
```

- [ ] **Step 2: Run command tests and verify RED**

Run: `uv run pytest tests/unit/sandbox/service/backends/test_opensandbox_backend.py -k command -v`

Expected: FAIL because `OpenSandboxBackend` does not exist.

- [ ] **Step 3: Implement minimal command mapping**

Build SDK options through the client facade, map logs and exit code, raise existing `NonZeroExitCodeError` and `CommandTimeoutError`, and emit metadata-only logs.

- [ ] **Step 4: Run command and routing tests**

Run: `uv run pytest tests/unit/sandbox/service/backends/test_opensandbox_backend.py -k command -v && uv run pytest tests/unit/sandbox/service/test_runtime_backend_routing.py -v`

Expected: PASS.

- [ ] **Step 5: Commit command support**

```bash
git add rock/sandbox/service/backends/opensandbox.py rock/sandbox/service/backends/__init__.py rock/sandbox/service/sandbox_proxy_service.py tests/unit/sandbox/service/backends/test_opensandbox_backend.py
git commit -m "feat(proxy): execute commands through OpenSandbox"
```

### Task 5: Implement OpenSandbox read, write, and streaming upload

**Files:**
- Modify: `rock/sandbox/service/backends/opensandbox.py`
- Modify: `tests/unit/sandbox/service/backends/test_opensandbox_backend.py`

**Interfaces:**
- Consumes: Task 3 `read_bytes`, `get_file_info`, and `write_file` primitives.
- Produces: ROCK-compatible `ReadFileResponse`, `WriteFileResponse`, and `UploadResponse`.

- [ ] **Step 1: Write failing read tests**

Test UTF-8, custom encoding, `errors="replace"`, `errors="ignore"`, and invalid encoding propagation. Assert the backend calls `read_bytes`, not SDK text decoding.

- [ ] **Step 2: Write failing permission tests**

Test new-path mode 644, existing-path mode preservation, and a metadata service error that aborts the write instead of assuming missing.

- [ ] **Step 3: Write failing streaming upload tests**

Pass a sentinel binary IO object whose `read(-1)` raises. Assert the exact object is passed to the client facade and the backend never buffers it.

```python
class NoReadAll(BytesIO):
    def read(self, size=-1):
        if size == -1:
            raise AssertionError("upload must not read the whole file")
        return super().read(size)
```

- [ ] **Step 4: Run file tests and verify RED**

Run: `uv run pytest tests/unit/sandbox/service/backends/test_opensandbox_backend.py -k 'read or write or upload' -v`

Expected: FAIL because file methods are not implemented.

- [ ] **Step 5: Implement file behavior**

Decode bytes in Admin with request defaults, preserve or choose mode, pass `UploadFile.file` through unchanged, and return existing ROCK response models. Keep path/size-only logging.

- [ ] **Step 6: Run backend and proxy tests**

Run: `uv run pytest tests/unit/sandbox/service/backends tests/unit/sandbox/service/test_runtime_backend_routing.py tests/unit/sandbox/test_proxy_enhancements.py -v`

Expected: PASS.

- [ ] **Step 7: Commit file support**

```bash
git add rock/sandbox/service/backends/opensandbox.py tests/unit/sandbox/service/backends/test_opensandbox_backend.py
git commit -m "feat(proxy): add OpenSandbox file operations"
```

### Task 6: Make single-sandbox status backend-aware

**Files:**
- Modify: `rock/sandbox/service/sandbox_proxy_service.py:1022-1090`
- Modify: `tests/unit/sandbox/service/test_runtime_backend_routing.py`

**Interfaces:**
- Consumes: fail-closed routing metadata and `OpenSandboxClient.get_state`.
- Produces: backend-aware `get_status` and `is_alive` with no fabricated Rocklet fields.

- [ ] **Step 1: Write failing status tests**

Cover OpenSandbox Pending and Running, state-machine transition, `is_alive`, `swe_rex_version=None`, no host/port fields, no Rocklet probe, and unchanged Rocklet behavior. Assert batch/list do not call OpenSandbox once per item.

- [ ] **Step 2: Run status tests and verify RED**

Run: `uv run pytest tests/unit/sandbox/service/test_runtime_backend_routing.py -k 'status or alive' -v`

Expected: FAIL because current status code always probes Rocklet when host IP exists and otherwise reports not alive.

- [ ] **Step 3: Implement backend-aware status**

Split status probing by backend. Reuse existing PENDING-to-RUNNING state-machine logic and return `swe_rex_version=None` for OpenSandbox. Do not add remote probes to batch/list.

- [ ] **Step 4: Run status and manager regression tests**

Run: `uv run pytest tests/unit/sandbox/service/test_runtime_backend_routing.py tests/unit/sandbox/test_proxy_enhancements.py tests/unit/sandbox/operator/opensandbox -v`

Expected: PASS.

- [ ] **Step 5: Commit status support**

```bash
git add rock/sandbox/service/sandbox_proxy_service.py tests/unit/sandbox/service/test_runtime_backend_routing.py
git commit -m "feat(proxy): route OpenSandbox status checks"
```

### Task 7: Documentation, full verification, live smoke, and PR preparation

**Files:**
- Modify: `docs/plans/opensandbox-operator-plan.md`
- Modify: `docs/plans/opensandbox-sdk-contract.md`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: verified branch and PR evidence linked to issue #1202.

- [ ] **Step 1: Update Phase 2 documentation**

Mark execute/read/write/upload/status delivered. Record sessions, portforward, scheduler policy, mixed-operator migration, and cancellation as deferred. Document shell warning, no Rocklet fallback, file permissions, and `use_server_proxy` behavior.

- [ ] **Step 2: Run formatting and lint**

Run: `uv run ruff format rock/sandbox/service/backends rock/sandbox/service/sandbox_proxy_service.py rock/sandbox/operator/opensandbox/client.py tests/unit/sandbox/service tests/unit/sandbox/operator/opensandbox`

Run: `uv run ruff check rock/sandbox/service/backends rock/sandbox/service/sandbox_proxy_service.py rock/sandbox/operator/opensandbox/client.py tests/unit/sandbox/service tests/unit/sandbox/operator/opensandbox`

Expected: both commands exit 0.

- [ ] **Step 3: Run relevant fast tests**

Run: `uv run pytest tests/unit/sandbox/service tests/unit/sandbox/operator/opensandbox tests/unit/sandbox/test_proxy_enhancements.py --reruns 1 -v`

Expected: PASS.

- [ ] **Step 4: Run project fast suite**

Run: `uv run pytest -m "not need_ray and not need_admin and not need_admin_and_network" --reruns 1`

Expected: PASS; record exact count and duration.

- [ ] **Step 5: Run real OpenSandbox smoke test**

Against the configured endpoint: create and wait for RUNNING; execute string and list commands; verify non-zero/check behavior; write/read with decode errors; stream a large binary upload and compare SHA-256; delete; capture evidence that no connection targeted port 22555. If unavailable, record the exact blocker and mark real E2E incomplete in the PR body.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/plans/opensandbox-operator-plan.md docs/plans/opensandbox-sdk-contract.md
git commit -m "docs: update OpenSandbox runtime capabilities"
```

- [ ] **Step 7: Self-review before push**

Run the repository's `pr-review-curator` workflow over `origin/master...HEAD`, filter findings against issue #1202 and the approved spec, fix accepted findings, rerun affected tests, and keep the local review report untracked.

- [ ] **Step 8: Push and create the follow-up PR**

```bash
git push -u fork feat/opensandbox-runtime-backend
gh pr create --repo alibaba/ROCK --base master --head zpzjzj:feat/opensandbox-runtime-backend --title "feat(proxy): add OpenSandbox runtime backend" --body-file /tmp/rock-opensandbox-runtime-pr.md
```

The PR body must include `Refs #1202`, test evidence, live E2E evidence or explicit incompleteness, deferred capabilities, and the no-Rocklet/fail-closed routing guarantee.

- [ ] **Step 9: Monitor reviews and CI**

Use the `github-pr-loop` watcher. Diagnose failures before rerunning, make atomic fixes, self-review before every push, reply to each actionable thread after the fix is pushed, and resolve only handled threads.
