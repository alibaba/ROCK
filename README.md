<div align="center">

<img src="./assets/rock.png" width="50%" alt="ROCK Logo">

# ROCK: Reinforcement Open Construction Kit

<h4>🚀 Reinforcement Learning Environment Toolkit 🚀</h4>

<p>
  <a href="https://github.com/alibaba/ROCK/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License">
  </a>
  <a href="https://github.com/alibaba/ROCK/issues">
    <img src="https://img.shields.io/github/issues/alibaba/ROCK" alt="GitHub issues">
  </a>
  <a href="https://github.com/alibaba/ROCK/stargazers">
    <img src="https://img.shields.io/github/stars/alibaba/ROCK?style=social" alt="Repo stars">
  </a>
</p>

</div>

## Table of Contents

1. [Introduction](#introduction)
2. [Core Features](#-core-features)
3. [Latest Updates](#-latest-updates)
4. [Quick Start](#-quick-start)
   - [Project Management](#project-management)
     - [Important Notes](#important-notes)
   - [Using GEM Protocol Environment](#using-gem-protocol-environment)
   - [Sandbox SDK Usage](#sandbox-sdk-usage)
5. [System Architecture](#-system-architecture)
   - [Technical Components](#technical-components)
     - [SDK Components](#sdk-components)
     - [Admin Management Server](#admin-management-server)
     - [Support Read-Write Separation Architecture](#support-read-write-separation-architecture)
   - [Core Technologies](#core-technologies)
   - [GEM Protocol Support](#gem-protocol-support)
6. [Configuration](#-configuration)
   - [Server Configuration](#server-configuration)
   - [Development Environment Configuration](#development-environment-configuration)
7. [Contribution](#-contribution)
   - [Development Setup](#development-setup)
   - [Reporting Issues](#reporting-issues)
   - [Code Style](#code-style)
8. [License](#-license)
9. [Acknowledgements](#-acknowledgements)

## Introduction

ROCK (Reinforcement Open Construction Kit) is a comprehensive sandbox environment management framework, primarily for reinforcement learning and AI development environments. It provides tools for building, running, and managing isolated containerized environments, suitable for development, testing, and research scenarios.

ROCK adopts a client-server architecture, uses Docker for containerization, and seamlessly integrates with modern development workflows. ROCK not only supports traditional sandbox management functions but also complies with the GEM protocol, providing standardized interfaces for reinforcement learning environments.

---

## 🚀 Core Features

* **Sandbox Management**: Create and manage isolated development environments with resource limits
* **SDK Interface**: Clean Python SDK interface supporting all core operations
* **GEM Protocol Compatible**: Compatible with GEM environment interface, providing unified access to reinforcement learning environments
* **Remote Execution**: Execute commands in remote sandbox environments via HTTP API
* **Auto Cleanup**: Automatically clean up idle sandboxes after configurable time
* **File Operations**: Upload and download files between sandbox environments
* **Concurrent Testing**: Supports launching multiple independent sandbox environments simultaneously for concurrent testing
* **Optional Read-Write Separation Architecture**: Supports optional read-write separation configuration to improve system performance and scalability

---

## 📢 Latest Updates

| 📣 Update Content |
|:-----------|
| **[Latest]** 🎉 ROCK Released |

---

## 🚀 Quick Start

### Project Management
ROCK uses `uv` for dependency management and virtual environment management, ensuring fast and consistent environment configuration:

```bash
# Clone repository
git clone https://github.com/alibaba/ROCK.git
cd ROCK

# Create virtual environment (using uv-managed Python)
uv venv --python 3.11 --python-preference only-managed

# Install dependencies using uv
uv sync --all-extras

# Activate virtual environment
source .venv/bin/activate
```

You can find more details in [quickstart.md](docs/docs/rock/quickstart.md)

#### Important Notes

**Important**: ROCK depends on Docker and uv tools for environment management.

1. **Python Environment Configuration**: To ensure ROCK can correctly mount the project and virtual environment along with its base Python interpreter, it is strongly recommended to use uv-managed Python environments to create virtual environments rather than system Python. This can be achieved through the `--python-preference only-managed` parameter.
If you don't want to use uv to manage the environment, you can refer to [installation.md](docs/docs/rock/installation.md) for installation and refer to [configuration.md](docs/docs/rock/configuration.md) for UV runtime environment startup.

2. **Distributed Environment Consistency**: In distributed multi-machine environments, please ensure that all machines use the same root Python interpreter for ROCK and uv Python configurations to avoid environment inconsistencies.

3. **Dependency Management**: Use the `uv` command to install all dependency groups, ensuring consistency between development, testing, and production environments.

4. **OS Support**: ROCK recommends managing environments on the same operating system, such as managing Linux image environments on a Linux system. However, it also supports cross-operating system level image management, for example, launching Ubuntu images on MacOS. For specific details, please refer to the MacOS Launch section in [quickstart.md](docs/docs/rock/quickstart.md)

### Using GEM Protocol Environment
ROCK is fully compatible with the GEM protocol, providing standardized environment interfaces:

```python
import rock
import random
# Create environment using GEM standard interface
env_id = "game:Sokoban-v0-easy"
env = rock.make(env_id)

# Reset environment
observation, info = env.reset(seed=42)

while True:
    # Interactive environment operations
    action = f"\\boxed{{{random.choice(['up', 'left', 'right', 'down'])}}}"
    observation, reward, terminated, truncated, info = env.step(action)

    if terminated or truncated:
        break

# Close environment
env.close()
```

### Sandbox SDK Usage
```python
import asyncio

from rock.actions import CreateBashSessionRequest
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig


async def run_sandbox():
    config = SandboxConfig(image="python:3.11", memory="8g", cpus=2.0)
    sandbox = Sandbox(config)

    await sandbox.start()
    await sandbox.create_session(CreateBashSessionRequest(session="bash-1"))
    result = await sandbox.arun(cmd="echo Hello Rock", session="bash-1")
    await sandbox.stop()


if __name__ == "__main__":
    asyncio.run(run_sandbox())
```

---

## 🛠️ System Architecture

### Technical Components

#### SDK Components
- **Sandbox Client**: Python SDK for interacting with remote sandbox environments
- **Environment Management**: Tools for building and managing development environments

#### Admin Management Server
Backend service for sandbox orchestration, supporting optional read-write separation architecture to improve performance and scalability:
- **Write Cluster**: Handles sandbox creation/destruction and other write operations
- **Read Cluster**: (Optional) Handles execution requests for existing sandboxes
- **API Endpoints**: RESTful API for sandbox management

#### Support Read-Write Separation Architecture
ROCK supports optional read-write separation architecture, routing different types of operations to dedicated server clusters to improve performance:

**Write Cluster Responsibilities**:
- Sandbox environment creation and destruction
- Other operations that modify system state

**Read Cluster Responsibilities** (Optional):
- Sandbox status queries
- Command execution
- File upload/download
- Session management operations

The read cluster is designed to reduce Ray pressure by directly managing sandboxes without going through an actor again, thereby improving overall system performance and response speed.

By default, ROCK uses a single cluster to handle all operations. When read-write separation is configured, the system routes read operations and write operations to different clusters to improve performance and scalability.

### Core Technologies
- **Container Management**: Container orchestration using Docker SDK
- **Web Framework**: Management services provided using FastAPI and uvicorn
- **Distributed Computing**: Distributed task processing using Ray
- **Concurrent Support**: Supports launching multiple independent sandbox environments simultaneously for concurrent testing, fully utilizing system resources

### GEM Protocol Support
ROCK is compatible with the GEM protocol, providing the following standard interfaces:
- `make(env_id)`: Create environment instance
- `reset(seed)`: Reset environment state
- `step(action)`: Execute action and return results

GEM environments follow standard return formats:
```python
# reset() returns
observation, info = env.reset()

# step() returns
observation, reward, terminated, truncated, info = env.step(action)
```

---

## 📄 Configuration

### Server Configuration
```bash
# Activate virtual environment
source .venv/bin/activate

# Start Rock service, local startup
admin --env local
```

> **Service Information**: The ROCK Local Admin service runs by default on `http://127.0.0.1:8080`. You can access this address through your browser to view the management interface.
For additional configuration information such as logs and Rocklet service startup methods, please refer to [configuration.md](docs/docs/rock/configuration.md)

### Development Environment Configuration
```bash
# Sync dependencies using uv
uv sync --all-extras --all-groups

# Run in virtual environment
source .venv/bin/activate

# Run tests
uv run pytest tests
```

## 🤝 Contribution

We welcome contributions from the community! Here's how to get involved:

### Development Setup
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

### Reporting Issues
Please use the GitHub issue tracker to report bugs or suggest features.

### Code Style
Follow existing code style and conventions. Please run tests before submitting pull requests.

---

## 📄 License

ROCK is distributed under the Apache License (Version 2.0). This product contains various third-party components under other open source licenses.

---

## 🙏 Acknowledgements

ROCK is developed by Alibaba Group. The rocklet component of our project is mainly based on SWE-ReX, with significant modifications and enhancements for our specific use cases. And we deeply appreciate the inspiration we have gained from the GEM project.

Special thanks to:

* [SWE-agent/SWE-ReX](https://github.com/SWE-agent/SWE-ReX)
* [axon-rl/gem](https://github.com/axon-rl/gem)

---

## 🤝 About [ROCK & ROLL Team]
ROCK is a project jointly developed by Taotian Future Living Lab and Alibaba AI Engine Team, with a strong emphasis on pioneering the future of Reinforcement Learning (RL). Our mission is to explore and shape innovative forms of future living powered by advanced RL technologies. If you are passionate about the future of RL and want to be part of its evolution, we warmly welcome you to join us! 

For more information about **ROLL**, please visit:
- [Official Documentation](https://alibaba.github.io/ROLL/)
- [GitHub Repository](https://github.com/alibaba/ROLL)

Learn more about the ROCK & ROLL Team through our official channels below👇

<a href="./assets/rock_wechat.png" target="_blank">
  <img src="https://img.shields.io/badge/WeChat-green?logo=wechat" alt="WeChat QR">
</a>


---

<div align="center">
Welcome community contributions! 🤝
</div>