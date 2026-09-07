---
sidebar_position: 10
---

# E2B Python SDK Compatibility

ROCK exposes a compatibility layer for a focused subset of the official E2B Python SDK. It lets an existing E2B
client create and inspect a ROCK sandbox, run a command, and perform common file operations without replacing the
client library.

This guide documents ROCK's E2B compatibility contract, which is intentionally narrower than the complete E2B API:

- supported and tested SDK version: `e2b==2.34.0`;
- version reported by every ROCK sandbox response: `envdVersion=0.3.0`;
- documented client: synchronous Python `Sandbox`.

See the official E2B documentation for the upstream concepts: [sandbox lifecycle](https://docs.e2b.dev/sandbox),
[commands](https://docs.e2b.dev/commands), and [filesystem](https://docs.e2b.dev/filesystem). The exact SDK baseline is
the official [`e2b` 2.34.0 source](https://github.com/e2b-dev/E2B/tree/43db96a0ef2e555b96eee1a52856013fbf0dc644).
ROCK pins that version in `pyproject.toml`, and its advertised envd version is defined in
`rock/common/constants.py`.

## Capability matrix

**Supported** means covered by the current SDK integration/live test. **Protocol-compatible** means the implemented
route satisfies the call but the call is not in live acceptance. **Partial** means material differences apply.

| Area | E2B Python API | Status | ROCK behavior |
|---|---|---|---|
| Lifecycle | `Sandbox.create()` | Partial | Creates a ROCK sandbox; only the parameters in [Create](#create) have effect. |
| Lifecycle | `sandbox.get_info()` / `Sandbox.get_info(id)` | Supported | Returns E2B `SandboxInfo`; ROCK-to-E2B state mapping is documented below. |
| Lifecycle | `Sandbox.list()` | Partial | Requires a non-empty metadata filter, returns running sandboxes only, and returns one unpaginated result set. |
| Lifecycle | `sandbox.kill()` / `Sandbox.kill(id)` | Supported | Irreversibly deletes the sandbox; returns `False` when it no longer exists. |
| Lifecycle | `sandbox.is_running()` | Protocol-compatible | `/health` returns `True` only while the ROCK state is `RUNNING`. |
| Lifecycle | `sandbox.set_timeout()` / `Sandbox.set_timeout(id, ...)` | Partial | Resets a running sandbox's existing ROCK auto-clear timeout. Seconds are rounded up to whole minutes, and regular command/file activity keeps ROCK's sliding renewal behavior. |
| Commands | `sandbox.commands.run()` in the foreground | Supported | Non-interactive only; returns stdout, stderr, and exit code and supports environment, working directory, and a finite timeout. |
| Commands | `sandbox.commands.run(background=True)` | Partial | The returned handle can wait on the original response stream; it cannot kill or reconnect to the process. |
| Commands | `CommandHandle.disconnect()` | Partial | Closes the response stream without killing the command; reconnecting to that command is not supported. |
| Files | `files.make_dir()` | Supported | Recursively creates parents; returns `False` when the directory already exists. |
| Files | `files.write()` / `files.write_files()` | Supported | Multipart text and binary writes, parent creation, and overwrite are supported. |
| Files | `files.read(format="text")` | Supported | Returns `str`. |
| Files | `files.read(format="bytes")` | Supported | Returns `bytearray`. |
| Files | `files.read(format="stream")` | Protocol-compatible | Uses the same streaming `GET /files` route, but is not in the current live acceptance test. |
| Files | `files.list()` / `files.get_info()` | Supported | Returns E2B `EntryInfo` for files and directories. |
| Files | `files.exists()` | Protocol-compatible | Reuses the implemented `Stat` RPC; not in the current live acceptance test. |
| Other | `sandbox.get_host()`, `upload_url()`, `download_url()` | Not supported | ROCK does not implement E2B wildcard traffic hosts or E2B signed URL verification. |
| Other | E2B template, volume, Code Interpreter, MCP, PTY, and snapshot APIs | Not supported | Required E2B routes or semantics are absent. |
| Other | `sandbox.git.*` helpers | Not guaranteed | Some helpers only call `commands.run()` and may work when Git is installed, but they are not part of the tested contract. |

The integration contract is exercised in `tests/integration/admin/test_e2b_sdk_commands_run.py` and the deployed
path in `tests/integration/admin/test_e2b_sdk_commands_run_live.py`.

## Deployment and client routing

ROCK separates the E2B control plane from the E2B sandbox data plane. Always configure both URLs explicitly; the
create response does not contain an E2B `domain`, so E2B wildcard-host discovery is not a supported deployment mode.

```bash
pip install "e2b==2.34.0"

export E2B_API_KEY="<rock-api-key>"
export E2B_API_URL="https://<rock-control-host>"
export E2B_SANDBOX_URL="https://<rock-data-host>"
export E2B_TEMPLATE_ID="<rock-template-id-or-image>"
```

The external gateway must route paths as follows:

| Client base URL | Path | ROCK role |
|---|---|---|
| `E2B_API_URL` | `POST /sandboxes` | Admin role (`ROCK_ADMIN_ROLE=admin`) |
| `E2B_API_URL` | `GET`, `DELETE /sandboxes/{sandboxID}` | Admin role |
| `E2B_API_URL` | `POST /sandboxes/{sandboxID}/timeout` | Admin role |
| `E2B_API_URL` | `GET /v2/sandboxes` | Proxy role, even though the SDK sends this list request to `api_url` |
| `E2B_SANDBOX_URL` | `/health`, `/process.Process/Start`, `/files`, `/filesystem.Filesystem/*` | Proxy role |

The split is defined by `rock/admin/main.py::_include_routers()`. A deployment that sends every
`E2B_API_URL` path to only the Admin role will make `Sandbox.list()` fail.

The proxy uses `E2b-Sandbox-Id` to select a running sandbox. Commands delegate to the configured sandbox proxy
backend. File methods specifically reach Rocklet through the sandbox `host_ip` and mapped `PROXY` port, so they are
unavailable for an OpenSandbox record that has no such mapping. For Connect streaming, the ingress must preserve the
long-lived HTTP response and disable response buffering.

ROCK expects the deployment gateway to authenticate both planes. The application returns the incoming
`X-API-Key` as `envdAccessToken`, after which the E2B SDK sends it to the data plane as `X-Access-Token`; the current
proxy does not independently validate that token. It also does not use `E2b-Sandbox-Port`. Set
`validate_api_key=False` when a ROCK key does not follow E2B's key format.

The implementation is in `rock/admin/entrypoints/e2b_api.py`, `rock/admin/entrypoints/e2b_proxy_api.py`, and
`rock/admin/service/e2b_proxy_service.py`.

### Backend and operating-system support

| ROCK runtime | Lifecycle APIs | `commands.run()` | `files.*` |
|---|---|---|---|
| Linux sandbox with Rocklet `PROXY` mapping | As listed in this guide | Supported | Supported as listed in this guide |
| OpenSandbox operator | Not end-to-end compatible: E2B create/get/list require a sandbox IP that this operator does not publish | The proxy can delegate execution for a pre-existing running record, but this path is not acceptance-tested | Not supported by the E2B files adapter |
| Windows sandbox | Control-plane creation is backend-dependent | Not supported by this E2B adapter | Not supported by this E2B adapter |

The command SDK always sends `/bin/bash -l -c`, and the file adapter always applies POSIX paths rooted at
`/home/user`. The data-plane compatibility contract therefore requires a Linux/POSIX sandbox with `/bin/bash`.
For non-OpenSandbox files, sandbox state must also contain a reachable `host_ip` and mapped Rocklet `PROXY` port.

## Sandbox lifecycle

### Create

Use `Sandbox.create()` rather than the deprecated constructor. It waits for ROCK to start the runtime and obtain its
IP. Use a control-plane `request_timeout` that covers image preparation and startup; `180` seconds is a reasonable
example, but the appropriate value is deployment-specific.

| Parameter | Status | Behavior |
|---|---|---|
| `template` | Supported | The SDK always sends a non-blank value (`base` when omitted). A ready ROCK template supplies its image and CPU/memory/disk values. If no ready template exists, ROCK treats the value as a raw image/manifest reference. |
| `timeout` | Partial | Omitted or `0` becomes the SDK default of 300 seconds before the request. ROCK requires a positive integer on the wire and rounds it up to whole minutes. Commands and file operations refresh the ROCK expiration deadline, so this is a sliding auto-stop timeout rather than a strict fixed-time E2B kill. |
| `metadata` | Supported | String-to-string metadata is preserved. An empty map is valid. `ap-sandbox-id`, when present, is also used as the requested container name. |
| `envs` | Supported | Passed to the sandbox and inherited by later `commands.run()` calls. Per-command `envs` values override matching sandbox values. |
| `secure` | Ignored | Accepted for SDK compatibility, but does not change ROCK runtime security or data-plane authentication. Enforce authentication at the gateway. |
| `allow_internet_access` | Ignored | Accepted, but ROCK does not alter egress policy from this field. |
| `lifecycle` / `autoPause` / `autoResume` | Not supported | Default SDK values are accepted but ignored. A non-default pause lifecycle can also send `autoPauseMemory`, which ROCK rejects with HTTP 400. |
| `mcp` | Not supported | A non-empty config is rejected with HTTP 400. An empty config is omitted on the wire but still triggers SDK-side MCP bootstrap, which is not supported. |
| `network` | Not supported | A non-empty config is rejected with HTTP 400; an empty config is omitted. Use ROCK deployment/network configuration instead. |
| `volume_mounts` | Not supported | Non-empty mounts are rejected with HTTP 400; an empty map is omitted. E2B volumes are not mapped to ROCK storage. |

The create response contains `sandboxID`, requested `templateID`, `clientID="rock"`, `envdVersion="0.3.0"`, and an
`envdAccessToken`. It does not contain `domain` or `trafficAccessToken`.

The request model and create mapping are in `rock/admin/proto/request.py` and
`rock/admin/service/e2b_service.py`.

#### Cold-start metadata

When no ready template is available, ROCK accepts the following optional fields in `metadata`. All values must
be strings. If a ready template is found, these fields are ignored and the template's resources are used.

| Field | Value | Default when omitted |
|---|---|---|
| `cpuCount` | Positive integer number of CPU cores, such as `"4"`. | 2 CPU cores |
| `memoryMB` | Positive integer memory size in MB, such as `"16384"` (16 GiB). | 8192 MB |
| `startup_timeout` | Positive finite number of seconds to wait for the sandbox to become ready after submission, such as `"120"`. | 85 seconds |

For example, use the following object as the `metadata` value in a `POST /sandboxes` request or `Sandbox.create()` call:

```json
{
  "cpuCount": "4",
  "memoryMB": "16384",
  "startup_timeout": "120"
}
```

Invalid values are rejected with HTTP 400 on cold starts. Resource requests remain subject to the deployment's
CPU and memory limits. `startup_timeout` covers readiness polling and status queries after submission; it does not
include preparation/submission time or change client/gateway HTTP timeouts. The top-level `timeout` continues to
control sandbox lifetime.

### Get sandbox information

Both `sandbox.get_info()` and `Sandbox.get_info(id, ...)` are supported. The returned `SandboxInfo` includes sandbox
ID, template/image, metadata, start/end times, state, CPU count, memory, and `envdVersion`. ROCK also reports disk
size on the wire and adds `e2b.agents.kruise.io/sandbox-ip` to metadata. The detail `template_id` is derived from
current sandbox state and can be the resolved image rather than the template string echoed by the create response.

ROCK maps states as follows:

| ROCK state | E2B state |
|---|---|
| `PENDING`, `RUNNING` | `RUNNING` |
| `STOPPED`, `ARCHIVED` | `PAUSED` |
| `ARCHIVING`, `DELETED`, missing, or unknown | Not found |

The `PAUSED` value is informational compatibility only: ROCK does not expose E2B `connect()`/resume, so a stopped or
archived sandbox cannot be restored through this SDK surface.

These responses require valid string metadata, a valid sandbox IP, resource values, and timezone-aware timestamps in
the ROCK record. Incomplete backend records fail instead of returning a partial `SandboxInfo`.

The sandbox field mapping is in `rock/admin/service/e2b_sandbox_info.py`.

### List sandboxes

ROCK supports only `Sandbox.list(query=SandboxQuery(metadata={...}))`, with these differences:

- at least one non-empty metadata key/value pair is required; plain `Sandbox.list()` returns HTTP 400;
- all metadata pairs are combined with exact-match AND semantics;
- only ROCK `RUNNING` sandboxes are returned;
- `query.state`, `limit`, and `next_token` are currently ignored;
- ROCK returns no `x-next-token`, so the SDK paginator has one page;
- the server does not apply a result limit. Use high-selectivity metadata, especially in large deployments.

The SDK's encoded metadata form is supported. The underlying endpoint also accepts `key:value,key2:value2` for
first-party integrations. Invalid, empty, or duplicate pairs return HTTP 400.

The `/v2/sandboxes` behavior is covered by `tests/unit/admin/entrypoints/test_e2b_proxy_api.py`.

### Kill and health

`sandbox.kill()` and `Sandbox.kill(id, ...)` map to irreversible ROCK deletion. Depending on the backend and state,
ROCK either deletes directly or stops the sandbox before deleting it. A repeated kill receives 404 and the E2B SDK
returns `False`.

`sandbox.is_running()` calls the data-plane `/health` route. It returns `True` only for `RUNNING`; pending, stopped,
archived, deleted, and missing sandboxes return `False`. This method is protocol-compatible but not part of the
current live SDK acceptance test.

### Set timeout

`sandbox.set_timeout(timeout)` and the static `Sandbox.set_timeout(id, timeout, ...)` form are supported for a
`RUNNING` sandbox. `timeout` is a strict integer number of seconds in the E2B `int32` range. ROCK maps it onto the
existing minute-based auto-clear model by rounding up to whole minutes; `0` marks the deadline immediately due.
Success is an empty HTTP 204 response, which the Python SDK exposes as `None`. Missing sandboxes return the E2B
`{code, message}` 404 shape.

E2B control-plane status checks used by `get_info()`, `set_timeout()`, and `kill()` do not refresh the timeout.
All other ROCK timeout behavior remains unchanged: command/file activity may renew the sliding auto-clear deadline,
the periodic lifecycle scan may introduce delay, and expiration stops the sandbox instead of guaranteeing E2B's
default irreversible kill.

### Unsupported lifecycle APIs

ROCK exposes no compatible routes for `Sandbox.connect()`/resume, `pause()`/`beta_pause()`, `fork()`,
`update_network()`, `get_metrics()`, snapshot operations, or E2B template management.

ROCK also does not provide the E2B wildcard-host traffic contract used by `sandbox.get_host()`, or the E2B signature
verification expected by `upload_url()` and `download_url()`. Use the regular supported `files.*` methods for file
transfer and the ROCK [Sandbox Proxy](./sandbox_proxy.md) for services exposed from a sandbox.

## Commands

ROCK supports the official [`commands.run()` shape](https://docs.e2b.dev/commands):

Only non-interactive `sandbox.commands.run()` calls are supported. Interactive stdin, PTY, and follow-up input are
not supported.

| Parameter | Status | Behavior |
|---|---|---|
| `cmd` | Supported | Executed as `/bin/bash -l -c <cmd>`. |
| `envs` | Supported | Merged over sandbox-level environment variables. |
| `cwd` | Supported | Passed to the runtime; an invalid directory produces `InvalidArgumentException`. |
| `timeout` | Partial | Use a finite value from greater than zero through **60 seconds**. The parser currently accepts up to 85 seconds, but values above 60 seconds are not part of the compatibility guarantee. `0` and `None` (unlimited) are rejected. |
| `request_timeout` | SDK-side only | Controls client/transport waiting; it does not extend ROCK's command execution timeout. |
| `on_stdout`, `on_stderr` | Partial | Callbacks receive output only after the command finishes because ROCK buffers stdout and stderr; real-time streaming is not provided. |
| `background=True` | Partial | Returns a synthetic process handle immediately. `handle.wait()` works while consuming the original stream. The PID is not a durable backend PID. |
| `stdin=False` or omitted | Supported | This is the only supported stdin mode. |
| `stdin=True` | Rejected | Returns Connect `unimplemented`; there is no interactive input stream. |
| `user` | Ignored | ROCK does not switch the runtime user for E2B commands. Omit this parameter. |

A zero exit code returns `CommandResult(stdout, stderr, exit_code=0)`. A non-zero exit is sent as a normal process
end event, after which the Python SDK raises `CommandExitException`; the exception carries `stdout`, `stderr`,
`exit_code`, and `error`. A command deadline raises `TimeoutException`. Invalid requests raise
`InvalidArgumentException`, and the per-proxy concurrency guard is surfaced as `RateLimitException`.

The request envelope is limited to 1 MiB. Each proxy process tracks at most 1,024 active E2B command tasks. Output
is buffered, so callers should avoid unbounded command output or unbounded concurrent commands. A client disconnect
does not cancel the task, but there is no later reconnection API; in a multi-worker deployment, keep the original
stream on the same proxy worker and configure load-balancer timeouts for at least the command duration.

The following are not supported: `commands.list()`, `commands.connect(pid)`, `commands.kill(pid)`,
`CommandHandle.kill()`, `commands.send_stdin()`, `CommandHandle.send_stdin()`, `CommandHandle.close_stdin()`, PTY
commands, and tagged commands.

`CommandHandle.disconnect()` does close the current response stream, but the backend command keeps running until it
exits or reaches its finite timeout. Because `commands.connect()` and `commands.kill()` are unavailable, its output
cannot be recovered and it cannot be controlled afterward.

See ROCK's `rock/admin/proto/e2b_connect.py` mapping and the official
[`e2b` 2.34.0 command source](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b/sandbox_sync/commands/command.py).

## Filesystem

The supported filesystem methods follow the official
[read/write](https://docs.e2b.dev/filesystem/read-write) and
[file information](https://docs.e2b.dev/filesystem/info) models.

### Paths and users

ROCK applies Linux/POSIX path rules:

- absolute paths remain absolute;
- relative paths are resolved below `/home/user`;
- `~` and `~/...` resolve to `/home/user`;
- `.` and `..` components are normalized;
- Windows path semantics are not supported.

ROCK does not implement E2B filesystem user switching. Omit `user` from every call. With the advertised
`envdVersion=0.3.0`, the SDK supplies its logical default user `user`; ROCK accepts that default for `/files` calls.
A different user is rejected by read/write HTTP routes, while the Connect filesystem routes currently ignore the
authorization user. Do not depend on that protocol inconsistency: operations run as the Rocklet process user.

### Create a directory

`make_dir(path, request_timeout=...)` recursively creates missing parents. It returns `True` on the first creation
and `False` if the directory already exists. If the path exists as a non-directory, the SDK raises
`InvalidArgumentException`. `path` must be non-empty.

### Write files

Default multipart upload supports `str` (UTF-8) and `bytes`, creates missing parent directories, and overwrites an
existing file. It returns `WriteInfo` for one file or `list[WriteInfo]` in input order for a batch. `WriteInfo`
contains `name`, normalized `path`, and `type=FileType.FILE`; file metadata is not returned.

Binary/text file-like `IO` objects use the same multipart path at `envdVersion=0.3.0` and are protocol-compatible,
but are not covered by the current live acceptance test. A batch can contain at most 256 file parts. Files are
forwarded to Rocklet sequentially and the batch is not atomic: an earlier write remains if a later write fails.
There is no E2B-specific total-byte or per-file limit, but large/high-concurrency uploads have not been capacity
qualified and should be bounded by the caller.

The following write options do not provide their upstream E2B semantics:

| Option | ROCK behavior |
|---|---|
| `gzip=True` | `envdVersion=0.3.0` makes the SDK fall back to an uncompressed multipart upload. |
| `use_octet_stream=True` | The SDK falls back to multipart; ROCK does not implement octet-stream upload. |
| non-empty `metadata={...}` | The SDK raises `TemplateException` before sending the request because file metadata requires a newer envd version. |
| `metadata={}` | Accepted, but stores no metadata. |

### Read files

The data plane streams bytes from Rocklet. The SDK buffers and decodes them for `format="text"`, or returns a
`bytearray` for `format="bytes"` (not `bytes`). A missing path raises `FileNotFoundException`; reading a directory
raises `InvalidArgumentException`.

`format="stream"` uses the same streaming response and is protocol-compatible, but is not in the current live
acceptance test. When using it, consume the iterator fully or close its context manager so the pooled connection is
released. The `gzip=True` read option may still return correct content, but ROCK does not guarantee compressed
transfer and the option should not be used as a bandwidth contract.

### List and inspect files

`files.list()` defaults to `depth=1`, accepts depths from 1 through 100, returns entries ordered by path, and is
limited to 10,000 entries per call. A larger result or an invalid depth raises `InvalidArgumentException`.

`files.list()` returns `list[EntryInfo]`; `files.get_info()` returns one `EntryInfo`. Each object contains:

- `name`, `path`, and `type`;
- `size` in bytes;
- numeric `mode` and string `permissions`;
- `owner` and `group`;
- timezone-aware `modified_time`;
- optional `symlink_target`.

ROCK maps regular files to `FileType.FILE` and directories to `FileType.DIR`. It does not expose a separate symlink
file type: a valid symbolic link is mapped to the target's file/directory type and can include a resolved
`symlink_target`. Custom E2B file metadata is not populated.

`files.exists()` reuses the same implemented `Stat` RPC and returns `True`/`False`; it is protocol-compatible but not
part of the current live acceptance test.

See the ROCK file routes in `rock/admin/entrypoints/e2b_proxy_api.py`, the Rocklet implementation in
`rock/rocklet/file_system.py`, and the official
[`e2b` 2.34.0 filesystem source](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b/sandbox_sync/filesystem/filesystem.py).

### Filesystem errors

| Condition | Python SDK result |
|---|---|
| Missing path passed to `read()`, `get_info()`, or `exists()` | `FileNotFoundException` (`exists()` converts it to `False`) |
| Missing/non-directory path passed to `list()`, empty/invalid path, invalid depth, or read/write on a directory | `InvalidArgumentException` |
| Sandbox not running on a file route | `FileNotFoundException` |
| Existing directory passed to `make_dir()` | Returns `False` |
| Proxy cannot reach the sandbox filesystem | Usually `TimeoutException` or a transport-level `SandboxException` |
| Permission failure | `SandboxException` |

`request_timeout` remains available on supported file calls as the E2B client request timeout. It does not change
the sandbox lifecycle timeout.

The following filesystem methods are not supported: `remove()`, `rename()`, and `watch_dir()`. No compatible E2B
routes are exposed for their RPCs.

E2B volume APIs, custom file metadata, network-mount watching, and filesystem operations through the OpenSandbox
operator are outside this compatibility contract.

## Synchronous SDK demo

This compact example creates a sandbox, runs a command, writes and reads a file, lists the sandbox by metadata, and
then deletes it. The metadata value is deliberately unique because ROCK requires a metadata filter for
`Sandbox.list()`.

```python
import os
import uuid

from e2b import Sandbox, SandboxQuery


request_id = f"e2b-rock-demo-{uuid.uuid4().hex}"
options = {
    "api_url": os.environ["E2B_API_URL"],
    "sandbox_url": os.environ["E2B_SANDBOX_URL"],
    "api_key": os.environ["E2B_API_KEY"],
    "validate_api_key": False,
    "request_timeout": 180,
}

sandbox = Sandbox.create(
    template=os.environ["E2B_TEMPLATE_ID"],
    timeout=300,
    metadata={"demo-request-id": request_id},
    envs={"SANDBOX_ENV": "from-create"},
    **options,
)

try:
    info = sandbox.get_info()
    print("sandbox:", info.sandbox_id, info.state)

    result = sandbox.commands.run(
        'printf "%s:%s\\n" "$SANDBOX_ENV" "$COMMAND_ENV"',
        envs={"COMMAND_ENV": "from-command"},
        stdin=False,
        timeout=60,
    )
    assert result.stdout == "from-create:from-command\n"
    assert result.exit_code == 0

    directory = f"/tmp/{request_id}"
    path = f"{directory}/hello.txt"
    sandbox.files.make_dir(directory)
    sandbox.files.write(path, "hello ROCK\n")
    assert sandbox.files.read(path) == "hello ROCK\n"
    print("file:", sandbox.files.get_info(path))

    paginator = Sandbox.list(
        query=SandboxQuery(metadata={"demo-request-id": request_id}),
        **options,
    )
    assert sandbox.sandbox_id in {item.sandbox_id for item in paginator.next_items()}
finally:
    sandbox.kill()
```

For application code, keep the `kill()` call in a `finally` block. A metadata lookup can find only a sandbox that has
already reached `RUNNING`; it is not a complete cleanup fallback for a creation failure in an earlier state.
