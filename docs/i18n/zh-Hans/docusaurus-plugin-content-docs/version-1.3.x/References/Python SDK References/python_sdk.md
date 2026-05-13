---
sidebar_position: 2
---

# Python SDK 参考

本指南详细介绍如何使用 ROCK SDK 进行开发，包括沙箱环境管理和 GEM 环境交互。

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

### 2.2 沙箱组管理

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

### 2.3 配置示例

```python
config = SandboxConfig(
    image="python:3.11",
    auto_clear_seconds=60 * 20,
    experiment_id="test",
)
```

### 2.4 沙箱加速配置

ROCK 提供沙箱网络加速功能，支持配置 APT、PIP 和 GitHub 镜像源，提升受限网络环境下的包下载速度。

#### 支持的加速类型

**APT 镜像配置**

配置 APT 包管理器镜像源，加速 Debian/Ubuntu 软件包下载。

```python
from rock.sdk.sandbox.speedup import SpeedupType

# 配置 APT 镜像
await sandbox.network.speedup(
    speedup_type=SpeedupType.APT,
    speedup_value="http://mirrors.cloud.aliyuncs.com"
)
```

**PIP 镜像配置**

配置 Python 包索引镜像，加速 pip 安装。

```python
# HTTP 镜像
await sandbox.network.speedup(
    speedup_type=SpeedupType.PIP,
    speedup_value="http://mirrors.cloud.aliyuncs.com"
)

# HTTPS 镜像
await sandbox.network.speedup(
    speedup_type=SpeedupType.PIP,
    speedup_value="https://mirrors.aliyun.com"
)
```

**GitHub 加速**

通过添加自定义 DNS 解析条目加速 GitHub 访问。

```python
await sandbox.network.speedup(
    speedup_type=SpeedupType.GITHUB,
    speedup_value="11.11.11.11"
)
```

#### 完整示例

```python
from rock.sdk.sandbox.speedup import SpeedupType
from rock.actions import RunMode

async def setup_sandbox_with_speedup():
    """创建沙箱并配置加速"""
    config = SandboxConfig(image="python:3.11")
    sandbox = Sandbox(config)
    
    await sandbox.start()
    
    # 配置加速（在安装包之前配置）
    await sandbox.network.speedup(
        speedup_type=SpeedupType.APT,
        speedup_value="http://mirrors.cloud.aliyuncs.com"
    )
    
    await sandbox.arun(cmd="apt-get update && apt-get install -y git", mode=RunMode.NOHUP)

    await sandbox.network.speedup(
        speedup_type=SpeedupType.PIP,
        speedup_value="https://mirrors.aliyun.com"
    )

    # speedup 不会主动安装 PIP，仅配置镜像源进行加速
    await sandbox.arun(cmd="pip install numpy", mode=RunMode.NOHUP)

    # 可以通过镜像 IP 加速 GitHub 访问
    await sandbox.network.speedup(
        speedup_type=SpeedupType.GITHUB,
        speedup_value="11.11.11.11"
    )

    return sandbox
```

#### 注意事项

1. **配置顺序**: 在安装包之前配置加速
2. **HTTPS vs HTTP**: HTTPS 镜像不需要为 PIP 配置 trusted-host
3. **GitHub IP**: 不同区域可能需要不同的 IP 以获得最佳性能
4. **持久性**: 配置在沙箱生命周期内持久有效
5. **多次调用**: 后续的加速调用会覆盖之前的配置
6. **PIP 安装**: speedup 功能仅配置镜像源，不会自动安装 PIP

## 3. GEM SDK

### 3.1 Python SDK 方式

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

## 4. Model Service SDK

### 4.1 概述

Model Service SDK 提供与 Model Service 交互的接口，支持 Agent 工作流中的 LLM 请求/响应处理。`ModelClient` 类是读取请求和写入响应到模型服务日志文件的主要接口。

### 4.2 基本用法

```python
import asyncio
from rock.sdk.model.client import ModelClient

async def main():
    # 创建 ModelClient 实例
    client = ModelClient()

    # 获取第一个请求 (index=0)
    first_request = await client.anti_call_llm(index=0)
    print(f"第一个请求: {first_request}")

    # 发送响应并获取下一个请求
    llm_response = '{"content": "你好，有什么可以帮你的？"}'
    next_request = await client.anti_call_llm(index=1, last_response=llm_response)
    print(f"下一个请求: {next_request}")

asyncio.run(main())
```

### 4.3 超时与取消支持

`pop_request` 和 `wait_for_first_request` 方法支持超时和取消，防止无限阻塞：

#### 超时配置

```python
import asyncio
from rock.sdk.model.client import ModelClient

async def main():
    client = ModelClient()

    try:
        # 等待第一个请求，超时时间为 30 秒
        await client.wait_for_first_request(timeout=30.0)

        # 弹出请求，超时时间为 60 秒（默认值）
        request = await client.pop_request(index=1)
    except TimeoutError as e:
        print(f"操作超时: {e}")

asyncio.run(main())
```

#### 取消处理

```python
import asyncio
from rock.sdk.model.client import ModelClient

async def main():
    client = ModelClient()

    async def get_request():
        try:
            request = await client.pop_request(index=1)
            return request
        except asyncio.CancelledError:
            print("请求被取消")
            raise

    # 创建可取消的任务
    task = asyncio.create_task(get_request())

    # 5 秒后取消
    await asyncio.sleep(5)
    task.cancel()

asyncio.run(main())
```

### 4.4 默认超时时间

轮询操作的默认超时时间为 **60 秒**。您可以自定义此值：

```python
# 使用默认超时（60 秒）
await client.pop_request(index=1)

# 自定义超时（30 秒）
await client.pop_request(index=1, timeout=30.0)

# 无超时（无限等待 - 不推荐）
await client.pop_request(index=1, timeout=None)
```

## 相关文档
- [快速开始指南](../../Getting%20Started/quickstart.md) - 了解如何快速开始使用 ROCK SDK
- [API 文档](../api.md) - 查看 SDK 封装的底层 API 接口
- [配置指南](../../User%20Guides/configuration.md) - 了解 SDK 相关的配置选项
- [安装指南](../../Getting%20Started/installation.md) - 详细了解 ROCK 安装和配置