---
sidebar_position: 2
---

# Python SDK 参考

本指南详细介绍如何使用 ROCK SDK 进行开发，包括沙箱环境管理和 GEM 环境交互。

## 目录

- [Python SDK 参考](#python-sdk-参考)
  - [目录](#目录)
  - [1. 概述](#1-概述)
  - [2. Sandbox SDK](#2-sandbox-sdk)
    - [2.1 基本沙箱操作](#21-基本沙箱操作)
    - [3.2 沙箱组管理](#32-沙箱组管理)
  - [相关文档](#相关文档)
    - [3.3 配置示例](#33-配置示例)
  - [4. GEM SDK](#4-gem-sdk)
    - [4.1 Python SDK 方式](#41-python-sdk-方式)

## 1. 概述

ROCK SDK为开发者提供了便捷的Python接口来使用ROCK平台的功能，包括沙箱环境管理和GEM环境交互。

> **重要提示**: 使用 SDK 之前，请确保 ROCK Admin 服务正在运行。可以通过以下命令启动：
> ```bash
> rock admin start
> ```

## 2. Sandbox SDK

### 2.1 基本沙箱操作

```python
import asyncio

from rock.actions import CreateBashSessionRequest
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

async def run_sandbox():
    """Run sandbox demo with admin server requirement.

    NOTE: This demo requires the admin server to be running for proper execution.
    Make sure to start the admin server before running this script.
    Default admin server port is 8080.
    """
    # Create sandbox configuration
    config = SandboxConfig(image="python:3.11", memory="8g", cpus=2.0)

    # Create sandbox instance
    sandbox = Sandbox(config)

    # Start sandbox (connects to admin server)
    await sandbox.start()

    # Create session in sandbox for command execution
    await sandbox.create_session(CreateBashSessionRequest(session="bash-1"))

    # Execute command in sandbox session
    result = await sandbox.arun(cmd="echo Hello ROCK", session="bash-1")
    print("\n" + "*" * 50 + "\n" + result.output + "\n" + "*" * 50 + "\n")

    # Stop and clean up sandbox resources
    await sandbox.stop()

if __name__ == "__main__":
    # Ensure admin server is running before executing
    print("IMPORTANT: Make sure the admin server is running before executing this demo!")
    print("Start the admin server with: rock admin start")
    asyncio.run(run_sandbox())
```

### 3.2 沙箱组管理

```python
from rock.sdk.sandbox.config import SandboxGroupConfig

# 创建沙箱组配置
config = SandboxGroupConfig(
    image="python:3.11",
    size=4,  # 创建4个沙箱
    start_concurrency=2,  # 并发启动级别为2
)

# 创建并启动沙箱组
sandbox_group = SandboxGroup(config)
await sandbox_group.start()

# 批量操作
for sandbox in sandbox_group.sandbox_list:
    await sandbox.run_in_session(Action(session="default", command="echo Hello"))

# 批量停止
await sandbox_group.stop()
```

## 相关文档

- [快速开始指南](../../Getting%20Started/quickstart.md) - 了解如何快速开始使用 ROCK SDK
- [API 文档](../api.md) - 查看 SDK 封装的底层 API 接口
- [配置指南](../../User%20Guides/configuration.md) - 了解 SDK 相关的配置选项
- [安装指南](../../Getting%20Started/installation.md) - 详细了解 ROCK 安装和配置

### 3.3 配置示例

```python
config = SandboxConfig(
    image="python:3.11",
    auto_clear_seconds=60 * 20,
    experiment_id="test",
)
```

## 4. GEM SDK

### 4.1 Python SDK 方式

```python
import random
import rock

def main():
    """Main function to run the Sokoban demo with admin server requirement.

    NOTE: This demo requires the admin server to be running for proper execution.
    Make sure to start the admin server before running this script.
    """
    # Create environment using GEM standard interface
    # NOTE: This requires the admin server to be running
    env_id = "game:Sokoban-v0-easy"
    env = rock.make(env_id)

    # Reset environment to initial state
    observation, info = env.reset(seed=42)
    print(
        "\n"
        + "=" * 80
        + "\nInitial Observation:\n"
        + str(observation)
        + "\n\nInitial Info:\n"
        + str(info)
        + "\n"
        + "=" * 80
        + "\n"
    )

    # Run environment loop until termination
    step_count = 0
    while True:
        # Interactive environment operation with random actions
        action = f"\\boxed{{{random.choice(['up', 'left', 'right', 'down'])}}}"
        observation, reward, terminated, truncated, info = env.step(action)

        step_count += 1
        print(
            "\n"
            + "-" * 80
            + f"\nStep {step_count} - Action: {action}\nReward: {reward}\nObservation:\n{observation}\nInfo: {info}\nTerminated: {terminated}, Truncated: {truncated}\n"
            + "-" * 80
            + "\n"
        )

        # Check if environment has reached terminal state
        if terminated or truncated:
            print("\n" + "=" * 80 + "\nEpisode finished!\n" + "=" * 80 + "\n")
            break

    # Clean up environment resources
    env.close()

if __name__ == "__main__":
    # Ensure admin server is running before executing
    print(
        "\n"
        + "=" * 80
        + "\nIMPORTANT: Make sure the admin server is running before executing this demo!\nStart the admin server with: rock admin start\n"
        + "=" * 80
        + "\n"
    )
    main()
```