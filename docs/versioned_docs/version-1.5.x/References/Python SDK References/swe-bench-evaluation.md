# SWE-Bench Evaluation

This guide demonstrates how to use the ROCK SDK to run SWE-Bench Verified evaluations, including sandbox setup, Agent integration, test environment preparation, and result parsing.

### Quick Start 

SWE-Bench is a benchmark for evaluating AI coding agents on real-world software engineering tasks. 

Running a SWE-Bench task on ROCK involves the following steps:

1. **load_task_config** — Load `task.yaml` to get the task instruction
2. **start_sandbox** — Start a sandbox with a task-specific Docker image
3. **agent.install / agent.run** — Install and run the Agent to solve the task
4. **setup_test_env** — Upload test files and run-test script to the sandbox
5. **Run tests** — Execute the test script via `sandbox.arun()` with timeout
6. **parse_swebench_result** — Parse test output to determine PASSED / FAILED
7. **sandbox.stop** — Clean up sandbox resources

**Here is an example code** 

```python
import asyncio
from pathlib import Path

from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

async def main():
    task_name = "django__django-14539"
    task_dir = Path("/root/terminal-bench-datasets/datasets/swebench-verified") / task_name
    agent_config_path = "/path/to/iflow_config.yaml"

    # 1. Load task instruction
    task_config = await load_task_config(task_dir)           # see load_task_config section
    instruction = task_config["instruction"]

    # 2. Start sandbox
    sandbox = await start_sandbox(task_name)                 # see start_sandbox section

    try:
        # 3. Install and run Agent
        await sandbox.agent.install(config=agent_config_path)
        result = await sandbox.agent.run(instruction)

        # 4. Setup test environment
        await setup_test_env(sandbox, task_dir)              # see setup_test_env section

        # 5. Run tests
        resp = await run_tests(sandbox)                      # see Running Tests section

        # 6. Parse results
        is_resolved = parse_swebench_result(resp.output)     # see parse_swebench_result section
        print(f"Task {task_name} resolved: {is_resolved}")
    finally:
        await sandbox.stop()

asyncio.run(main())
```

The following sections describe each function used in the workflow in detail.

---

## start_sandbox

Start a sandbox instance with a task-specific SWE-Bench Docker image. Each task has a pre-built image containing the target repository and environment.

The `image` parameter follows the format:

```
slimshetty/swebench-verified:sweb.eval.x86_64.{task_name}
```

For example, task `django__django-14539` maps to:

```
slimshetty/swebench-verified:sweb.eval.x86_64.django__django-14539
```

```python
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

async def start_sandbox(task_name: str) -> Sandbox:
    image = f"slimshetty/swebench-verified:sweb.eval.x86_64.{task_name}"
    config = SandboxConfig(image=image)
    sandbox = Sandbox(config)
    await sandbox.start()
    return sandbox
```

## load_task_config

Load task configuration from a `task.yaml` file in the task directory. The YAML file contains the `instruction` field that describes the coding task for the Agent.

```python
import yaml
from pathlib import Path

async def load_task_config(task_dir: Path) -> dict:
    task_yaml_path = task_dir / "task.yaml"
    if not task_yaml_path.exists():
        raise FileNotFoundError(f"task.yaml not found in {task_dir}")

    with open(task_yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config

# Usage
task_config = await load_task_config(task_dir)
instruction = task_config["instruction"]
```

## agent.install / agent.run

Use `sandbox.agent.install()` and `sandbox.agent.run()` to deploy and execute an Agent inside the sandbox. Refer to [Rock Agent](./rock-agent.md) for detailed Agent configuration.

```python
# Install Agent with a YAML configuration file（e.g., iflow_config.yaml）
await sandbox.agent.install(config="iflow_config.yaml")

# Run Agent with the task instruction
result = await sandbox.agent.run(instruction)
```

## setup_test_env

Prepare the test environment in the sandbox: install the [uv](https://github.com/astral-sh/uv) package manager, and upload test files and the run-test script.

```python
from pathlib import Path

from rock.actions.sandbox.request import CreateBashSessionRequest
from rock.sdk.sandbox.client import RunMode, Sandbox

async def setup_test_env(sandbox: Sandbox, task_dir: Path) -> str:
    """Set up the test environment and return the session name."""
    # 1. Create a session with custom environment variables
    session_name = "swe-evaluation"
    await sandbox.create_session(
        CreateBashSessionRequest(
            session=session_name,
            env_enable=True,
            env={
                "UV_PYTHON_INSTALL_MIRROR": "https://registry.npmmirror.com/-/binary/python-build-standalone"
            },
        )
    )

    # 2. Install uv
    for cmd in [
        "wget https://github.com/astral-sh/uv/releases/download/0.10.5/uv-x86_64-unknown-linux-gnu.tar.gz",
        "tar -xzf uv-x86_64-unknown-linux-gnu.tar.gz --strip-components=1 -C /usr/local/bin",
    ]:
        await sandbox.arun(cmd, session=session_name, mode=RunMode.NOHUP)

    # 3. Upload test files
    sandbox_test_dir = "/tests"
    result = await sandbox.fs.upload_dir(task_dir / "tests", sandbox_test_dir)
    if result.exit_code != 0:
        raise RuntimeError("Failed to upload test files")

    # 4. Upload run-tests script
    run_tests_script = task_dir / "run-tests.sh"
    result = await sandbox.upload_by_path(
        run_tests_script,
        f"{sandbox_test_dir}/{run_tests_script.name}",
    )
    if not result.success:
        raise RuntimeError("Failed to upload run-tests script")

    return session_name
```

## Running Tests

Execute the test script with a configurable timeout using `RunMode.NOHUP`.

```python
import shlex
from rock.actions.sandbox.response import Observation
from rock.sdk.sandbox.client import RunMode

test_timeout_sec = 3600
sandbox_test_dir = "/tests"

session_name = "swe-evaluation"

run_tests_command = f"sh -c 'bash {sandbox_test_dir}/run-tests.sh'"
resp: Observation = await sandbox.arun(
    run_tests_command,
    session=session_name,
    mode=RunMode.NOHUP,
    wait_timeout=test_timeout_sec,
)
```

## parse_swebench_result

Parse the test output to determine whether the SWE-Bench task is resolved. The parser looks for a result block delimited by marker lines and checks for `PASSED`.

```python
import re

def parse_swebench_result(output: str) -> bool:
    """Parse SWE-Bench test output to determine if the task is resolved.

    Matches the block between 'SWEBench results starts here' and
    'SWEBench results ends here', then checks whether it contains 'PASSED'.
    """
    match = re.search(
        r"SWEBench results starts here\s*(.*?)\s*SWEBench results ends here",
        output,
        re.DOTALL,
    )
    if not match:
        return False
    return match.group(1).strip() == "PASSED"

# Usage
is_resolved = parse_swebench_result(resp.output)
```

## Notes

- **Task Datasets**: Task directories (containing `task.yaml`, `tests/`, and `run-tests.sh`) can be obtained from the [terminal-bench-datasets](https://github.com/laude-institute/terminal-bench-datasets) repository.
- **Task Images**: Each SWE-Bench task requires a specific Docker image (e.g., `sweb.eval.x86_64.<task_name>`). Ensure the image is available before running tests.
- **Agent Config**: The Agent configuration YAML defines the runtime, dependencies, and execution command. See [Rock Agent](./rock-agent.md) for details.

