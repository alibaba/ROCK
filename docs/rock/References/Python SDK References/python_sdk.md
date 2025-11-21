---
sidebar_position: 2
---

# Python SDK Reference

This guide provides detailed information on how to use the ROCK SDK for development, including sandbox environment management and GEM environment interaction.

## Table of Contents

- [Python SDK Reference](#python-sdk-reference)
  - [Table of Contents](#table-of-contents)
  - [1. Overview](#1-overview)
  - [2. Sandbox SDK](#2-sandbox-sdk)
    - [2.1 Basic Sandbox Operations](#21-basic-sandbox-operations)
    - [3.2 Sandbox Group Management](#32-sandbox-group-management)
    - [3.4 Configuration Example](#34-configuration-example)
  - [Related Documents](#related-documents)
  - [3. GEM SDK](#3-gem-sdk)
    - [3.1 Python SDK Approach](#31-python-sdk-approach)

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

### 3.2 Sandbox Group Management

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

### 3.4 Configuration Example

```python
config = SandboxConfig(
    image="python:3.11",
    auto_clear_seconds=60 * 20,
    experiment_id="test",
)
```

## Related Documents

- [Quick Start Guide](../../Getting%20Started/quickstart.md) - Learn how to quickly get started with the ROCK SDK
- [API Documentation](../api.md) - View the underlying API interfaces encapsulated by the SDK
- [Configuration Guide](../../User%20Guides/configuration.md) - Learn about SDK-related configuration options
- [Installation Guide](../../Getting%20Started/installation.md) - Detailed information about ROCK installation and setup

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