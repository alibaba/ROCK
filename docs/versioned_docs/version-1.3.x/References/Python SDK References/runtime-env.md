# RuntimeEnv

The RuntimeEnv module is used to manage language runtime environments in the sandbox (currently providing Python / Node.js).

## Quick Start (Example)

```python
from rock.sdk.sandbox import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.runtime_env import RuntimeEnv, NodeRuntimeEnvConfig

sandbox_config = SandboxConfig()
sandbox = Sandbox()
await sandbox.start()

node_runtime_env_config = NodeRuntimeEnvConfig(version="default")
env = await RuntimeEnv.create(sandbox, node_runtime_env_config)

await env.run("node --version")
```

## RuntimeEnv.create

An async factory method that creates and initializes a RuntimeEnv instance based on the configuration, and automatically registers it to `sandbox.runtime_envs`.

```python
from rock.sdk.sandbox.runtime_env import RuntimeEnv, NodeRuntimeEnvConfig

env = await RuntimeEnv.create(
    sandbox,
    NodeRuntimeEnvConfig(version="22.18.0"),
)

# Auto-registered; accessible via sandbox.runtime_envs[env.runtime_env_id]
print(env.runtime_env_id in sandbox.runtime_envs)  # True
```

## wrapped_cmd

Wraps a command by adding `bin_dir` to PATH to ensure executables from the runtime environment are used with priority.

```python
wrapped = env.wrapped_cmd("node script.js")
# Returns: bash -c 'export PATH=/tmp/rock-runtime-envs/node/22.18.0/xxx/runtime-env/bin:$PATH && node script.js'
```

## run

Executes a command within the runtime environment. Internally implemented based on `wrapped_cmd`.

```python
await env.run("node script.js")
await env.run("npm install express")
```

## PythonRuntimeEnvConfig

| Field | Type | Default | Description |
|------|------|--------|------|
| `type` | `Literal["python"]` | `"python"` | Type identifier |
| `version` | `"3.11" \| "3.12" \| "default"` | `"default"` | Python version; default is 3.11 |
| `pip` | `list[str] \| str \| None` | `None` | List of pip packages or a requirements.txt path |
| `pip_index_url` | `str \| None` | Environment variable | pip index mirror |
| `extra_symlink_dir` | `str \| None` | `None` | Target directory for executable symlinks |
| `extra_symlink_executables` | `list[str]` | `["python", "python3", "pip", "pip3"]` | List of executables to symlink |

## NodeRuntimeEnvConfig

| Field | Type | Default | Description |
|------|------|--------|------|
| `type` | `Literal["node"]` | `"node"` | Type identifier |
| `version` | `"22.18.0" \| "default"` | `"default"` | Node version; default is 22.18.0 |
| `npm_registry` | `str \| None` | `None` | npm registry mirror |
| `extra_symlink_dir` | `str \| None` | `None` | Target directory for executable symlinks |
| `extra_symlink_executables` | `list[str]` | `["node", "npm", "npx"]` | List of executables to symlink |

## Constraints for Custom RuntimeEnv Implementations

A custom RuntimeEnv must follow these rules:

1. **Define the `runtime_env_type` class attribute**: used as a type identifier for automatic registration into the RuntimeEnv factory
2. **Override `_get_install_cmd()`**: return the install command
3. **The install command must end with**: renaming the directory to `runtime-env`

## Simplified NodeRuntimeEnv Implementation Example

```python
from rock.sdk.sandbox.runtime_env import RuntimeEnv, RuntimeEnvConfig
from typing import Literal
from pydantic import Field
from typing_extensions import override

# Config class: defines the config type so RuntimeEnv.create() can route to the corresponding implementation
class NodeRuntimeEnvConfig(RuntimeEnvConfig):
    type: Literal["node"] = "node"  # Must match runtime_env_type

# RuntimeEnv implementation class: defines how to install and run this runtime environment
class NodeRuntimeEnv(RuntimeEnv):
    runtime_env_type = "node"  # Auto-registered to RuntimeEnv._REGISTRY

    @override
    def _get_install_cmd(self) -> str:
        # Download the Node binary tarball and extract it, then rename to runtime-env
        return (
            "wget -q -O node.tar.xz https://npmmirror.com/mirrors/node/v22.18.0/node-v22.18.0-linux-x64.tar.xz && "
            "tar -xf node.tar.xz && "
            "mv node-v22.18.0-linux-x64 runtime-env"
        )
```

## Speeding Up Base Runtime Installation

`PythonRuntimeEnv` downloads Python packages from https://github.com/astral-sh/python-build-standalone/releases/ by default. If the network is unavailable or slow, you can override the default install command via `ROCK_RTENV_PYTHON_V31114_INSTALL_CMD` or `ROCK_RTENV_PYTHON_V31212_INSTALL_CMD` (e.g., switch to an internal registry or a mirror).

Default value example:

```python
"ROCK_RTENV_PYTHON_V31114_INSTALL_CMD": lambda: os.getenv(
    "ROCK_RTENV_PYTHON_V31114_INSTALL_CMD",
    "[ -f cpython31114.tar.gz ] && rm cpython31114.tar.gz; [ -d python ] && rm -rf python; "
    "wget -q -O cpython31114.tar.gz https://github.com/astral-sh/python-build-standalone/releases/download/20251120/cpython-3.11.14+20251120-x86_64-unknown-linux-gnu-install_only.tar.gz "
    "&& tar -xzf cpython31114.tar.gz && mv python runtime-env",
),
```

For example, override it to download from a mirror:

```bash
export ROCK_RTENV_PYTHON_V31114_INSTALL_CMD='[ -f cpython31114.tar.gz ] && rm cpython31114.tar.gz; [ -d python ] && rm -rf python; wget -q -O cpython31114.tar.gz https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/20251209/cpython-3.11.14+20251209-x86_64-unknown-linux-gnu-install_only.tar.gz && tar -xzf cpython31114.tar.gz && mv python runtime-env'
```

Make sure the command creates a `runtime-env` directory under the default working directory of `runtime_env`, and that `${workdir}/runtime-env/bin/` contains the expected executables, e.g.:

- `${workdir}/runtime-env/bin/python`

The same applies to Node.js: you can override the install command via `ROCK_RTENV_NODE_V22180_INSTALL_CMD` to use a faster download/install method.