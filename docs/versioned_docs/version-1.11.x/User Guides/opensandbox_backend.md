---
sidebar_position: 6
---

# OpenSandbox Backend

The OpenSandbox operator lets ROCK delegate sandbox lifecycle and runtime operations to an external
[OpenSandbox](https://github.com/alibaba/OpenSandbox) deployment. ROCK clients continue to call the ROCK Admin API;
they do not need backend-specific code.

## Architecture

With `runtime.operator_type: opensandbox`, ROCK Admin uses the OpenSandbox Python SDK for both:

- lifecycle operations through the OpenSandbox server; and
- command, file, session, and exposed-service operations through OpenSandbox `execd` endpoints.

OpenSandbox sandboxes do not need Rocklet. ROCK does not install, discover, or fall back to Rocklet for this backend.
The sandbox metadata field `extended_params.backend` is authoritative, and an invalid or conflicting backend fails
closed.

## Installation

Install the Admin dependencies, which include the supported OpenSandbox SDK:

```bash
pip install "rl-rock[admin]"
```

For a source checkout:

```bash
uv sync --extra admin
```

## Configuration

Configure one Admin deployment to use one operator type:

```yaml
runtime:
  operator_type: opensandbox

opensandbox:
  endpoint: opensandbox.example.com:8090
  protocol: https
  api_key: ""                  # Prefer OPEN_SANDBOX_API_KEY in the environment.
  runtime: docker              # Informational; the OpenSandbox server selects the runtime.
  image_registry_prefix: ""    # Optional prefix for image names without an explicit registry.
  use_server_proxy: false
  default_timeout: 600

scheduler:
  enabled: false
```

`endpoint` is the OpenSandbox server domain and optional port, without a URL path. Set `protocol` separately.
If `api_key` is empty, the OpenSandbox SDK reads `OPEN_SANDBOX_API_KEY`.

`use_server_proxy` controls how command and file requests reach `execd`:

- `false` uses endpoints returned by the OpenSandbox server. ROCK Admin must be able to reach those endpoints.
- `true` routes requests through the OpenSandbox server. Enable it only when that deployment supports server-proxy
  mode.

ROCK follows this setting strictly and does not retry through the other route.

## Client Usage

The existing ROCK SDK remains backend-transparent:

```python
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.actions import Command

sandbox = Sandbox(
    SandboxConfig(
        image="python:3.11",
        cpus=2,
        memory="4g",
        base_url="http://rock-admin.example.com:8080",
    )
)

await sandbox.start()
result = await sandbox.execute(Command(command="python -V"))
print(result.stdout)
```

The Admin service stores both the ROCK sandbox ID and the OpenSandbox ID. Callers continue to use only the ROCK ID.

## Capability Matrix

| Capability | OpenSandbox backend | Notes |
|---|---|---|
| Create, status, list | Supported | Creation returns `pending`; ROCK polls the OpenSandbox lifecycle state. |
| Delete a running sandbox | Supported | Maps to irreversible OpenSandbox `kill`. |
| Stop, restart | Not supported | Pause/resume requires persistence configured at creation time and is not exposed by this ROCK backend. |
| Archive, restore, image commit | Not supported | These paths depend on ROCK-managed worker storage or Ray actors. |
| Execute command | Supported | `cwd`, explicit command environment, timeout, and exit-code checks are mapped to `execd`. |
| Read, write, upload file | Supported | Upload passes the file stream to the SDK without buffering the whole file in Admin memory. |
| Persistent command session | Supported | Session-name mappings are stored in Redis so different Admin workers can reuse the same session. |
| Interactive session commands | Not supported | `expect` and interactive command/quit modes are rejected explicitly. |
| HTTP service proxy | Supported | ROCK resolves the requested sandbox port through OpenSandbox and preserves required endpoint headers. |
| WebSocket service proxy | Supported | Uses the same endpoint-discovery contract as HTTP proxy. |
| Raw TCP port forwarding over WebSocket | Not supported | The backend rejects this operation explicitly. |
| Worker maintenance scheduler | Not applicable | ROCK skips the Ray/Rocklet worker scheduler even if `scheduler.enabled` is true. |

### Session Environment and User

An OpenSandbox session inherits the environment of its sandbox/container. ROCK never copies the Admin process
environment across this boundary, including when the SDK request has `env_enable=true`.

Use the session request's explicit `env` and `startup_source` fields to initialize values and shell files. Those
commands run once when the session is created, and later commands in that session observe their effects.

OpenSandbox cannot switch the session user through ROCK. If `remote_user` is supplied, ROCK verifies it against
`id -un`; the request succeeds only when it already matches the sandbox's effective user.

## Service Proxy

Applications listening inside the sandbox can be reached with the existing ROCK proxy routes. A target port can be
selected through the path form `/proxy/{sandbox_id}/port/{port}/{path}`, the `X-ROCK-Target-Port` header, or the
`rock_target_port` query parameter. Specify the port through only one of these mechanisms.

ROCK asks the OpenSandbox lifecycle service for the endpoint and forwards the returned authorization headers. The
endpoint may be a direct address or a server-proxy address; ROCK does not assume a particular hostname-routing
strategy.

## Operational Notes

- Use a shared Redis deployment for multiple Admin workers; persistent session mappings are stored there.
- Do not enable Rocklet-specific worker maintenance tasks for this operator. Admin logs a warning and skips that
  scheduler when OpenSandbox is selected.
- Treat `use_server_proxy`, endpoint reachability, and authentication as deployment contracts. ROCK does not silently
  fall back between routes or backends.
- Do not mix Ray/Kubernetes and OpenSandbox sandboxes behind one Admin deployment. Select the operator at deployment
  level.
