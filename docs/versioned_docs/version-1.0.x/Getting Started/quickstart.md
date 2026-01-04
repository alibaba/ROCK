---
sidebar_position: 2
---

# Getting Started

This guide will demonstrate how to use ROCK to create and manage reinforcement learning environments through complete examples.

## Table of Contents

- [Getting Started](#getting-started)
  - [Table of Contents](#table-of-contents)
  - [1. Environment Preparation](#1-environment-preparation)
    - [1.1 System Requirements](#11-system-requirements)
    - [1.2 Verify Dependency Installation](#12-verify-dependency-installation)
    - [1.3 Project Initialization](#13-project-initialization)
  - [2. Activate Virtual Environment](#2-activate-virtual-environment)
  - [3. Verify Environment Configuration](#3-verify-environment-configuration)
  - [4. Start ROCK Service](#4-start-rock-service)
  - [5. Run Example Environments](#5-run-example-environments)
    - [5.1 Example Descriptions](#51-example-descriptions)
  - [6. Distributed Environment Configuration (Optional)](#6-distributed-environment-configuration-optional)
  - [7. MacOS Startup](#7-macos-startup)
  - [8. Starting from Pip Source](#8-starting-from-pip-source)
  - [Summary](#summary)
  - [Next Steps](#next-steps)

## 1. Environment Preparation

We recommend starting ROCK on Linux systems to maximize dependency reuse and improve environment startup speed. If you need to try on macOS, please refer to the [MacOS Startup](#7-macos-startup) section.

Before starting, please ensure your system has the following dependencies installed:

### 1.1 System Requirements

- **Docker**: ROCK uses Docker for containerized environment management
- **uv**: ROCK uses uv for dependency management and virtual environment creation

### 1.2 Verify Dependency Installation

```bash
# Verify Docker installation
docker --version

# Verify Docker image, and example depends on python:3.11 image
docker pull python:3.11

# Verify uv installation
uv --version
```

### 1.3 Project Initialization

```bash
# Clone repository
git clone <repository>
cd ROCK

# Create virtual environment (using uv-managed Python, use python 3.11 as an example)
uv venv --python 3.11 --python-preference only-managed

# Install all dependency groups
uv sync --all-extras
```

> **Important Note**: To ensure ROCK can correctly mount the project and virtual environment along with its base Python interpreter, it is strongly recommended to use uv-managed Python environments to create virtual environments rather than system Python.

## 2. Activate Virtual Environment

Before running any ROCK commands, you need to activate the virtual environment. Ensure sys.base_prefix is a uv-managed environment, such as `/root/.local/share/uv/python/cpython-3.11.8-linux-x86_64-gnu` or similar paths.

```bash
# Activate virtual environment
source .venv/bin/activate

# Verify Python environment
python -c "import sys; print('Base prefix:', sys.base_prefix)"
```

> **Verification Point**: Ensure the output base prefix path points to a uv-managed Python environment, not system Python.

## 3. Verify Environment Configuration

After activating the virtual environment, verify that dependencies are installed correctly:

```bash
# Check key dependencies
python -c "import rock; print("Hello ROCK")
```

## 4. Start ROCK Service

After activating the virtual environment, start the ROCK Admin service on project root:

```bash
# Ensure virtual environment is activated
source .venv/bin/activate

# Start ROCK Admin service (local environment)
rock admin start
```

After the service starts, you will see output similar to the following:

```
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8080 (Press CTRL+C to quit)
```

> **Service Information**: The ROCK Admin service runs by default on `http://127.0.0.1:8080`.

## 5. Run Example Environments

Now you can run example environments to verify the installation. Ensure the ROCK service is running, then open a new terminal window to execute the following commands:

```bash
# Ensure virtual environment is activated
source .venv/bin/activate

# Run sandbox example
python examples/sandbox_demo.py

# Run GEM protocol example
python examples/sokoban_demo.py
```

### 5.1 Example Descriptions

- **sandbox_demo.py**: Demonstrates how to use ROCK's sandbox SDK to create and manage containerized environments
- **sokoban_demo.py**: Demonstrates how to use ROCK's GEM protocol compatible interface to create reinforcement learning environments

> **Running Requirements**: Ensure the ROCK Admin service is running, as examples need to communicate with the service.

## 6. Distributed Environment Configuration (Optional)

For distributed multi-machine environments, ensure the following configurations are consistent:

1. All machines use the same root Python interpreter for ROCK and uv Python configurations
2. Docker versions are consistent across all nodes
3. Network configuration allows normal communication between nodes


## 7. MacOS Startup

On macOS, if you need to start Linux image environments, you first need to set the environment variable:

```bash
export ROCK_WORKER_ENV_TYPE=uv
```

During container startup, the corresponding uv environment will be installed. For details, please refer to the `rock/rocklet/docker_run_with_uv.sh` script.

> **Note**: Compared to Linux systems, the startup speed on macOS will be slower and more dependent on network conditions. You can adjust the script according to actual conditions.You can find detatils for ROCK_WORKER_ENV_TYPE in [Configuration Guide](../User%20Guides/configuration.md).

## 8. Starting from Pip Source

If starting the Admin Server from Pip source, after completing the ROCK installation by referring to [installation.md](./installation.md), you need to set an additional environment variable:

```bash
export ROCK_WORKER_ENV_TYPE=pip
```

(This startup method will pull and install the latest rocklet from the PyPI source when starting the container environment. The startup speed is relatively slow, so it is only recommended for testing purposes. For production environments, other startup methods are still recommended.)

## Summary

Congratulations! You have successfully completed the ROCK quick start guide. You should now be able to:

- Properly set up the ROCK development environment
- Use uv-managed Python environments
- Start and manage ROCK services
- Run example programs to verify installation
- Configure ROCK in distributed environments (if needed)

For a deeper understanding of ROCK's additional features, please refer to the following documents:

## Next Steps

- [Configuration Guide](../User%20Guides/configuration.md) - Detailed information about ROCK configuration options
- [API Documentation](../References/api.md) - View complete API interfaces
- [Python SDK Documentation](../References/Python%20SDK%20References/python_sdk.md) - Learn how to use the Python SDK for development
- [Installation Guide](./installation.md) - Detailed information about ROCK installation and setup
- [Overview](../overview.md) - Understand ROCK's overall architecture and design philosophy