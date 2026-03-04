---
sidebar_position: 2
---

# Python SDK Reference

This guide provides detailed information on how to use the ROCK SDK for development, including sandbox environment management and GEM environment interaction.

## 1. Overview

ROCK SDK provides developers with convenient Python interfaces to use ROCK platform features, including sandbox environment management and GEM environment interaction.

> **Important Note**: Before using the SDK, ensure that the ROCK Admin service is running. You can start it with the following command:
> ```bash
> rock admin start
> ```

## 2. Sandbox SDK

### 2.1 Basic Sandbox Operations

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

### 2.2 Sandbox Group Management

```python
from rock.sdk.sandbox.config import SandboxGroupConfig

# Create sandbox group configuration
config = SandboxGroupConfig(
    image="python:3.11",
    size=4,  # Create 4 sandboxes
    start_concurrency=2,  # Concurrency level for startup is 2
)

# Create and start sandbox group
sandbox_group = SandboxGroup(config)
await sandbox_group.start()

# Batch operations
for sandbox in sandbox_group.sandbox_list:
    await sandbox.run_in_session(Action(session="default", command="echo Hello"))

# Batch stop
await sandbox_group.stop()
```

### 2.3 Configuration Example

```python
config = SandboxConfig(
    image="python:3.11",
    auto_clear_seconds=60 * 20,
    experiment_id="test",
)
```

### 2.4 Sandbox Speedup Configuration

ROCK provides sandbox network acceleration capabilities, supporting configuration of APT, PIP, and GitHub mirror sources to improve package download speeds in restricted network environments.

#### Supported Speedup Types

**APT Mirror Configuration**

Configure APT package manager mirror sources for faster Debian/Ubuntu package downloads.

```python
from rock.sdk.sandbox.speedup import SpeedupType

# Configure APT mirror
await sandbox.network.speedup(
    speedup_type=SpeedupType.APT,
    speedup_value="http://mirrors.cloud.aliyuncs.com"
)
```

**PIP Mirror Configuration**

Configure Python package index mirrors for faster pip installations.

```python
# HTTP mirror
await sandbox.network.speedup(
    speedup_type=SpeedupType.PIP,
    speedup_value="http://mirrors.cloud.aliyuncs.com"
)

# HTTPS mirror
await sandbox.network.speedup(
    speedup_type=SpeedupType.PIP,
    speedup_value="https://mirrors.aliyun.com"
)
```

**GitHub Acceleration**

Configure GitHub IP acceleration by adding custom DNS resolution entries.

```python
await sandbox.network.speedup(
    speedup_type=SpeedupType.GITHUB,
    speedup_value="11.11.11.11"
)
```

#### Complete Example

```python
from rock.sdk.sandbox.speedup import SpeedupType
from rock.actions import RunMode

async def setup_sandbox_with_speedup():
    """Create sandbox and configure acceleration"""
    config = SandboxConfig(image="python:3.11")
    sandbox = Sandbox(config)
    
    await sandbox.start()
    
    # Configure acceleration (before installing packages)
    await sandbox.network.speedup(
        speedup_type=SpeedupType.APT,
        speedup_value="http://mirrors.cloud.aliyuncs.com"
    )
    
    await sandbox.arun(cmd="apt-get update && apt-get install -y git", mode=RunMode.NOHUP)

    await sandbox.network.speedup(
        speedup_type=SpeedupType.PIP,
        speedup_value="https://mirrors.aliyun.com"
    )

    # Speedup does not automatically install PIP, it only configures mirror sources for acceleration
    await sandbox.arun(cmd="pip install numpy", mode=RunMode.NOHUP)

    # GitHub can be accelerated through mirror IP
    await sandbox.network.speedup(
        speedup_type=SpeedupType.GITHUB,
        speedup_value="11.11.11.11"
    )

    return sandbox
```

#### Important Notes

1. **Configuration Order**: Configure speedup before installing packages
2. **HTTPS vs HTTP**: HTTPS mirrors don't require trusted-host configuration for PIP
3. **GitHub IP**: Different regions may require different IPs for optimal performance
4. **Persistence**: Configurations persist within the sandbox lifecycle
5. **Multiple Calls**: Subsequent speedup calls will override previous configurations
6. **PIP Installation**: The speedup feature only configures mirror sources and does not automatically install PIP

## 3. GEM SDK

### 3.1 Python SDK Approach

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

## Related Documents
- [Quick Start Guide](../../Getting%20Started/quickstart.md) - Learn how to quickly get started with the ROCK SDK
- [API Documentation](../api.md) - View the underlying API interfaces encapsulated by the SDK
- [Configuration Guide](../../User%20Guides/configuration.md) - Learn about SDK-related configuration options
- [Installation Guide](../../Getting%20Started/installation.md) - Detailed information about ROCK installation and setup