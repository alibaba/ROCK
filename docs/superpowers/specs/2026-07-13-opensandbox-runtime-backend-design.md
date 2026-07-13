# OpenSandbox Runtime Backend Design

## Goal

Complete the first part of Phase 2 of issue #1202 by allowing ROCK's existing proxy APIs to execute commands and manipulate files in OpenSandbox-managed sandboxes without installing Rocklet in those sandboxes.

The public ROCK SDK and API remain backend-neutral. Existing Ray, Kubernetes, Docker, local, and remote deployments continue to use Rocklet with no behavior change.

## Scope

This change includes:

- a runtime backend interface expressed in ROCK request and response models;
- extraction of the existing Rocklet HTTP behavior into a Rocklet backend;
- an OpenSandbox SDK backend for command execution, file access, and streaming upload;
- backend selection from `SandboxInfo.extended_params["backend"]`;
- backend-aware single-sandbox `get_status` and `is_alive` behavior;
- explicit errors for unsupported OpenSandbox operations.

This change does not include:

- OpenSandbox WebSocket port forwarding;
- OpenSandbox persistent command sessions;
- changes to Rocklet-only scheduler tasks;
- changes to public ROCK SDK or API models;
- lifecycle behavior already delivered by PR #1203;
- enabling OpenSandbox pause or resume without explicit persistence support;
- unrelated proxy or scheduler refactoring.

## Architecture

### Runtime backend boundary

Introduce a narrow `SandboxRuntimeBackend` `Protocol`, whose methods accept existing ROCK models and return existing ROCK response models. It represents only the capabilities delivered in this PR:

- execute a command;
- read and write files;
- upload a file;

Sessions and port forwarding remain outside the initial protocol. `SandboxProxyService` resolves the backend before dispatch and rejects those capabilities for OpenSandbox with clear 4xxx ROCK errors, without attempting a Rocklet connection.

`SandboxProxyService` creates the default backend registry from `RockConfig` and accepts an optional injected registry for tests and future assembly. OpenSandbox imports remain lazy so a Rocklet-only Admin does not require the SDK.

### Rocklet backend

`RockletBackend` owns the current HTTP transport behavior that is embedded in `SandboxProxyService`, including:

- host and proxy-port URL construction;
- sandbox and trace headers;
- JSON, form, and multipart requests;
- the existing HTTP 511 exception response mapping;
- the existing HTTP 504 timeout response mapping;
- current network-error behavior.

The extraction is a behavior-preserving refactor. Backward compatibility for missing backend metadata applies only to deployments whose configured operator is a legacy Rocklet-backed operator. An OpenSandbox deployment must never fall back to Rocklet because OpenSandbox-created sandboxes are not required to contain or expose Rocklet.

### OpenSandbox backend

`OpenSandboxBackend` uses the async OpenSandbox Python SDK. It reads `extended_params["opensandbox_id"]`, connects to the existing sandbox using the configuration delivered in Phase 1, and translates between ROCK models and OpenSandbox SDK models.

The backend extends the Phase 1 `OpenSandboxClient` facade with stable runtime primitives instead of accessing raw SDK handles. The client owns lazy SDK loading, connection configuration, connect/close, SDK model construction, and SDK exception translation. The backend owns ROCK request/response mapping and compatibility behavior.

Sandbox handles are not cached. Each operation connects with `skip_health_check=True` and closes its temporary handle in `finally`. `OpenSandboxClient` owns one shared HTTP transport, passes it to SDK connection configuration, and closes it from `SandboxProxyService.aclose()`.

### Backend routing

`SandboxProxyService` remains responsible for retrieving sandbox status and selecting a backend. Selection follows these rules:

1. sandbox metadata is the primary routing source;
2. `backend == "rocklet"` selects Rocklet only when compatible with the configured operator;
3. `backend == "opensandbox"` selects OpenSandbox only when compatible with the configured operator and never falls back to Rocklet;
4. missing backend metadata selects Rocklet only when the configured operator is a legacy Rocklet-backed operator;
5. missing backend metadata under the OpenSandbox operator produces an explicit bad-request error, even if `opensandbox_id` exists;
6. a metadata/operator conflict or unknown backend produces an explicit bad-request error.

This fail-closed rule prevents incomplete OpenSandbox metadata from being misreported as an unreachable Rocklet service on port 22555. It also makes loss of `backend` or `opensandbox_id` visible at the routing boundary.

The ROCK sandbox ID remains the external and storage primary key. The OpenSandbox ID is an implementation detail stored in `extended_params`. A single Admin instance does not support mixed-operator migration; switching the global operator requires existing sandboxes to be cleaned up or migrated first.

Backend routing occurs before transport-specific validation. Runtime operations require `State.RUNNING`; PENDING returns an explicit not-ready error and the proxy does not implement its own lifecycle polling. Rocklet validates host IP and port mapping, while OpenSandbox validates `opensandbox_id` and never requires or fabricates Rocklet network fields.

## Operation Mapping

### Command execution

ROCK command fields map to OpenSandbox command options as follows:

- string commands pass through unchanged;
- list commands are converted with shell-safe joining;
- `cwd` maps to the working directory;
- `env` maps to command environment variables;
- timeout seconds map to a duration;
- synchronous ROCK execution uses foreground OpenSandbox execution.

OpenSandbox stdout messages are concatenated into ROCK stdout, stderr messages into ROCK stderr, and the exit code is preserved. ROCK's existing `check` behavior remains observable to callers.

OpenSandbox always executes a shell command string. When ROCK receives a string command with `shell=False`, the backend preserves OpenSandbox behavior and emits a warning without logging the command text. List commands use `shlex.join()` to preserve argument boundaries. A non-zero exit returns normally when `check=False` and raises the same `NonZeroExitCodeError` as Rocklet when `check=True`. Timeouts map to the same `CommandTimeoutError`.

### File operations

Reads use `read_bytes()` and decode in Admin with ROCK's `encoding` and `errors` semantics. This preserves caller-visible decoding behavior that the OpenSandbox text API cannot express.

Writes and uploads query existing file metadata first. Existing files retain their mode; new files use `0644`. A real metadata query failure is not treated as a missing file.

Upload passes FastAPI's binary `UploadFile.file` object directly to SDK `write_file()`. It never calls `file.read()` or buffers the full body. OpenSandbox 0.1.13 streams with chunked multipart for direct execd access and uses seekable, Content-Length multipart in server-proxy mode.

### Status and readiness

Single-sandbox `get_status` and `is_alive` route by backend. OpenSandbox uses the existing lifecycle `get_state()` primitive and retains the current PENDING-to-RUNNING state-machine transition. It does not probe Rocklet or populate host IP, port mapping, or `swe_rex_version`. Batch/list endpoints continue to read metadata without issuing one remote request per sandbox.

## Connection and timeout policy

`use_server_proxy` selects exactly one OpenSandbox network path. The backend does not retry through the other path because automatic fallback changes network policy and can repeat non-idempotent commands.

`OpenSandboxConfig.default_timeout` maps to SDK `ConnectionConfig.request_timeout` and must be positive. Per-command `Command.timeout` remains the execd process timeout. SSE reads retain the SDK behavior of no client read timeout and rely on the process timeout.

Request cancellation closes local handle resources but does not promise to interrupt an already-running remote command. Cross-backend cancellation semantics are deferred.

## Error Handling

Caller and configuration errors produce ROCK 4xxx errors, including:

- unknown backend names;
- missing backend metadata under the OpenSandbox operator;
- backend metadata that conflicts with the configured operator;
- missing `opensandbox_id` for an OpenSandbox sandbox;
- PENDING or otherwise non-running sandboxes;
- unsupported OpenSandbox sessions;
- unsupported OpenSandbox port forwarding.

OpenSandbox SDK and service errors pass through the centralized translation introduced by Phase 1. The backend must not leak raw SDK exceptions through the public proxy API.

The Rocklet backend preserves current 511, 504, and network-error behavior during extraction. OpenSandbox command, timeout, and SDK failures are translated to existing ROCK exception types; raw SDK exceptions are never exposed.

## Logging and observability

Logs include operation, trace context, safe identifiers, duration, result, and exit code. They do not include command text, file content, environment values, API keys, or endpoint headers. File operations may log path and size. The `shell=False` compatibility warning does not echo command text.

## Testing Strategy

Implementation follows TDD:

1. Characterization tests lock current Rocklet URL, headers, payload, and error behavior before extraction.
2. Routing tests cover legacy missing metadata, OpenSandbox missing metadata, metadata/operator conflicts, explicit backends, and unknown backends. They prove that no Rocklet request is attempted for an OpenSandbox sandbox.
3. State tests cover RUNNING, PENDING, backend-aware `get_status/is_alive`, and absence of fabricated Rocklet fields.
4. OpenSandbox command tests cover string warnings, list joining, field/result mapping, non-zero exits, `check`, timeouts, and translated failures.
5. File tests cover decode error strategies, new/existing permissions, streamed upload without `read()`, direct and server-proxy modes, and resource cleanup on success, error, and cancellation.
6. Capability tests prove OpenSandbox session and portforward calls fail clearly without Rocklet traffic.
7. Existing proxy and OpenSandbox operator tests run as regression coverage.

Required verification includes Ruff and the relevant fast pytest suites. A real OpenSandbox smoke test must cover create, wait for RUNNING, string/list commands, non-zero/check behavior, write/read decoding, streamed large-file upload with hash verification, delete, and evidence that port 22555 was never contacted. If credentials or environment prevent this test, the PR must explicitly disclose that real E2E remains incomplete.

Before push, the complete PR diff is self-reviewed. CI is monitored after the PR is created, and code-related failures or actionable review comments are handled in follow-up commits.

## Commit and PR Structure

Use atomic commits:

1. `docs: design OpenSandbox runtime backend`
2. `refactor(proxy): extract Rocklet runtime backend`
3. `feat(proxy): add fail-closed runtime backend routing`
4. `feat(opensandbox): add runtime client operations`
5. `feat(proxy): add OpenSandbox command and file backend`

The follow-up PR targets `master`, references issue #1202, and does not modify or reopen merged PR #1203.
