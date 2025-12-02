---
sidebar_position: 4
---

# Configuration

This guide provides detailed instructions on how to configure the ROCK environment to meet different usage requirements, including local development, testing, and production deployment.

## Table of Contents

- [Environment Variable Configuration](#1-environment-variable-configuration)
  - [Runtime Environments](#11-runtime-environments)
  - [Logging Configuration](#12-logging-configuration)
- [Distributed Deployment Requirements](#2-distributed-deployment-requirements)

## 1. Environment Variable Configuration

ROCK supports configuring key parameters through environment variables. The main environment variables are as follows:

```bash
export ROCK_BASE_URL=http://localhost:8080  # ROCK service base URL
export ROCK_LOG_LEVEL=INFO  # Log level
export ROCK_LOGGING_PATH=/path/to/logs  # Log file path, default None (output to console)
export ROCK_LOGGING_FILE_NAME=rocklet.log  # Log file name, default "rocklet.log"
export ROCK_LOGGING_LEVEL=INFO  # Log output level, default "INFO"
export ROCK_WORKER_ENV_TYPE=local  # Runtime environment type, options: local, docker, uv, pip
```

More environment variables can be found in `rock/env_vars.py`.

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

## Related Documents

- [Quick Start Guide](../Getting%20Started/quickstart.md) - Learn how to quickly set up the ROCK environment
- [API Documentation](../References/api.md) - View sandbox-related API interfaces
- [Python SDK Documentation](../References/Python%20SDK%20References/python_sdk.md) - Learn how to use the SDK to configure sandboxes
- [Installation Guide](../Getting%20Started/installation.md) - Detailed information about ROCK installation and setup