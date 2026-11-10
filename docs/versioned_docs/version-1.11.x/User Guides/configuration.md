---
sidebar_position: 4
---

# Configuration

This guide provides detailed instructions on how to configure the ROCK environment to meet different usage requirements, including local development, testing, and production deployment.

## 1. Environment Variable Configuration

ROCK supports configuring key parameters through environment variables. The main environment variables are as follows:

```bash
export ROCK_BASE_URL=http://localhost:8080  # ROCK service base URL
export ROCK_LOG_LEVEL=INFO  # Log level
export ROCK_LOGGING_PATH=/path/to/logs  # Log file path, default None (output to console)
export ROCK_LOGGING_FILE_NAME=rocklet.log  # Log file name, default "rocklet.log", can be customized by admin like admin.log
export ROCK_LOGGING_LEVEL=INFO  # Log output level, default "INFO"
export ROCK_WORKER_ENV_TYPE=local  # Runtime environment type, options: local, docker, uv, pip
```

More environment variables can be found in `rock/env_vars.py`.

### Python SDK defaults

Explicit `SandboxConfig` / `SandboxGroupConfig` values take precedence over environment variables. Resource defaults are read when each config is constructed; an unset variable retains the fallback below. Command timing defaults are read when the operation runs. Explicit `arun()` or `run_nohup_and_wait()` arguments override their wait defaults, including explicit zero values.

| Variable | Fallback | Meaning |
|---|---|---|
| `ROCK_DEFAULT_IMAGE` | `python:3.11` | Sandbox image |
| `ROCK_DEFAULT_MEMORY` | `8g` | Sandbox memory |
| `ROCK_DEFAULT_CPUS` | `2` | Sandbox CPUs (float) |
| `ROCK_DEFAULT_AUTO_CLEAR_SECONDS` | `300` | Sandbox automatic cleanup, seconds |
| `ROCK_DEFAULT_GROUP_SIZE` | `2` | Sandbox group size |
| `ROCK_DEFAULT_GROUP_START_CONCURRENCY` | `2` | Group startup concurrency |
| `ROCK_DEFAULT_GROUP_START_RETRY_TIMES` | `3` | Group startup retry count |
| `ROCK_SANDBOX_STATUS_CHECK_INTERVAL_SECONDS` | `3` | Start/restart status poll interval, seconds |
| `ROCK_SANDBOX_WAIT_TIMEOUT_SECONDS` | `300` | Default nohup completion timeout, seconds |
| `ROCK_SANDBOX_WAIT_INTERVAL_SECONDS` | `10` | Default nohup completion poll interval, seconds |
| `ROCK_SANDBOX_NOHUP_COMMAND_TIMEOUT_SECONDS` | `30` | Nohup submission timeout, seconds |
| `ROCK_SANDBOX_MIN_WAIT_INTERVAL_SECONDS` | `5` | Lower bound on process completion polling, seconds |

All timing variables use seconds. Status polling and nohup submission timeouts accept fractional seconds. The process polling interval is still capped by the sandbox auto-clear window, as before. Use positive timing values; changing these defaults does not change server-side scheduling policies. `ROCK_DEFAULT_CLUSTER` remains supported with its existing `vpc-nt-a` fallback.

```python
import os
from rock.sdk.sandbox.config import SandboxConfig

os.environ["ROCK_DEFAULT_CPUS"] = "0.5"
assert SandboxConfig().cpus == 0.5
assert SandboxConfig(cpus=4).cpus == 4
```

### 1.1 Runtime Environments

ROCK provides multiple different runtime environments to meet the needs of different scenarios, configured through the `ROCK_WORKER_ENV_TYPE` environment variable. Each environment has different deployment requirements, performance characteristics and applicable scenarios. Each environment has its own unique advantages and limitations, and developers can choose the most suitable runtime environment according to their deployment needs.

#### 1.1.1 Docker Runtime Environment

The Docker runtime environment is suitable for Docker image environments where dependencies are pre-installed. This environment requires the `/tmp/miniforge/bin/rocklet` executable to be directly available in the deployment environment.

**Mount Configuration:**
- `/tmp/miniforge` - Contains pre-installed Python environment
- `/tmp/local_files` - Contains local files required for execution

**Start Command:**
```bash
chmod +x /tmp/local_files/docker_run.sh && /tmp/local_files/docker_run.sh
```

**Use Cases:**
- Containerized deployment environments
- Already built custom Docker image containing `rocklet`
- Suitable for production, fast startup

**Requirements:**
- Requires a custom Docker image containing `/tmp/miniforge/bin/rocklet` executable
- Docker environment support

#### 1.1.2 Local Runtime Environment

The local runtime environment directly uses the Python environment and project files of the current deployment. This environment requires the same operating system between the host and container to directly mount the virtual environment and Python interpreter.

**Mount Configuration:**
- `python_env_path` - Python environment path
- `project_root` - Project root directory
- `.venv` - Virtual environment directory (mounted as `/tmp/miniforge` in container)
- `local_files` - Local files required for execution

**Start Command:**
```bash
chmod +x /tmp/local_files/docker_run.sh && /tmp/local_files/docker_run.sh
```

**Use Cases:**
- Development environments
- Scenarios where host and target container use the same operating system
- Need to quickly reuse existing Python environment

**Requirements:**
- Same operating system (host/container)
- Direct access to the currently deployed `.venv` virtual environment
- Python interpreter path compatibility

#### 1.1.3 UV Runtime Environment

The UV runtime environment only depends on the available ROCK project, but initialization is relatively slow and network requirements are higher. This environment is most suitable for scenarios without preconfigured environments. It rebuilds the rocklet environment from the original project. This is the recommended environment for Mac OS.

**Mount Configuration:**
- `project_root` - Project root directory (mounted as `/tmp + project_root` in container)
- `local_files` - Local files required for execution

**Start Command:**
```bash
chmod +x /tmp/local_files/docker_run_with_uv.sh && /tmp/local_files/docker_run_with_uv.sh '<container_project_root>'
```

**Use Cases:**
- Mac OS
- Cross-OS startup
- Scenarios without preconfigured environment
- No uv management Rock

**Advantages:**
- No pre-built image required
- Good cross-platform compatibility
- Suitable for development and testing especially

**Limitations:**
- Initialization is relatively slow
- Higher network requirements
- Longer startup time

#### 1.1.4 PIP Runtime Environment

The PIP runtime environment uses pip to install required dependencies in the container. This environment is suitable for quick setup and scenarios where dependencies can be installed in the container. It is the default runtime environment. It does not require pre-built images containing dependencies, and manages Python packages directly through pip.

**Mount Configuration:**
- `local_files` - Contains local files required for execution

**Start Command:**
```bash
chmod +x /tmp/local_files/docker_run_with_pip.sh && /tmp/local_files/docker_run_with_pip.sh
```

**Use Cases:**
- ROCK installation from PIP source
- Fast testing of ROCK

**Advantages:**
- Simple deployment setup

**Limitations:**
- Long dependency installation time
- Requires network access to install dependency packages
- Dependencies need to be installed each time on startup

#### 1.1.5 Configuration Guide

Refer to the following selection guide for different use cases:

| Scenario | Recommended Environment | Reason |
|----------|--------------------------|-------|
| Production environment | Docker Runtime | Fast startup, stable performance |
| Development environment, same OS | Local Runtime | Environment reuse, fast development cycle |
| Mac development | UV Runtime | Best cross-platform compatibility support |
| Cross-platform development | UV Runtime | Avoids environment compatibility issues |
| Fast testing | UV Runtime | Requires no pre-configuration |
| PIP source installation | PIP Runtime | Install dependencies directly with pip |

These runtime environments are configured through the `ROCK_WORKER_ENV_TYPE` environment variable, which can be set to "local", "docker", "uv" or "pip".

### 1.2 Logging Configuration

Regarding logging configuration, ROCK's logging system has the following characteristics:

- The logging system cannot output to both file and console simultaneously. If `ROCK_LOGGING_PATH` is set, logs will be output to the designated file, otherwise to console.
- `ROCK_LOGGING_LEVEL` is used to control the output log level, while `ROCK_LOG_LEVEL` is used for general log level settings.

## 2. Distributed Deployment Requirements

Since ROCK supports distributed deployment, when running on different nodes of a Ray cluster, the following consistency requirements must be met:

#### Directory Structure Consistency

On all Ray nodes, the following directory structure must be completely consistent:
- ROCK project repository directory
- `.venv` virtual environment directory
- The base Python directory that `.venv` depends on


#### Mounting Requirements

ROCK's startup depends on mounting the ROCK project and the corresponding base Python environment, requiring consistency in multi-machine environments:

#### Verifying Distributed Configuration

Distributed deployment configuration can be verified through the following methods:

```bash
# Check directory consistency on all nodes
ls -la /path/to/rock
ls -la /path/to/rock/.venv
ls -la $ROCK_PYTHON_ENV_PATH

# Verify Python environment availability
$ROCK_PYTHON_ENV_PATH/bin/python --version

# Check environment variable settings on all nodes
echo $ROCK_PYTHON_ENV_PATH
echo $ROCK_PROJECT_ROOT
```

## 3. Sandbox Lifecycle Configuration

ROCK 1.11 uses a persisted state machine for sandbox creation, stop, restart, archive, restore, automatic expiration, and deletion. Lifecycle timing and archive storage are configured under `lifecycle`.

See [Sandbox Lifecycle and State Transition Control](lifecycle.md) for the complete state diagram, configuration defaults, transition rules, failure recovery, and backend capability differences.

## Related Documents

- [Quick Start Guide](../Getting%20Started/quickstart.md) - Learn how to quickly set up the ROCK environment
- [API Documentation](../References/api.md) - View sandbox-related API interfaces
- [Python SDK Documentation](../References/Python%20SDK%20References/python_sdk.md) - Learn how to use the SDK to configure sandboxes
- [Installation Guide](../Getting%20Started/installation.md) - Detailed information about ROCK installation and setup
- [Sandbox Lifecycle and State Transition Control](lifecycle.md) - Configure and operate the complete sandbox lifecycle
