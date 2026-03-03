"""SWE-bench Verified evaluation demo.

This script runs a single SWE-bench Verified task end-to-end:
  1. Loads the task configuration (instruction & tests) from a local task directory.
  2. Starts a sandbox with the corresponding SWE-bench Docker image.
  3. Installs and runs an agent inside the sandbox to resolve the task.
  4. Executes the task's test suite and reports whether the fix is correct.

Prerequisites:
  - The ROCK admin server must be running (`rock admin start`).
  - Fill in the agent configuration in iflow_config.yaml.
  - Clone the SWE-bench Verified tasks repository:
    * git clone https://github.com/laude-institute/terminal-bench-datasets.git

Usage:
  1. Set `tasks_folder` and `task_name` in the __main__ block below.
  2. Run:
       python -m examples.evaluation.swe_bench.swe_bench_verified_demo
"""

import sys
import asyncio
from pathlib import Path

from examples.evaluation.swe_bench.common import load_task_config, parse_swebench_result, setup_test_env, start_sandbox
from rock.actions.sandbox.request import CreateBashSessionRequest
from rock.actions.sandbox.response import Observation
from rock.logger import init_logger
from rock.sdk.sandbox.client import RunMode, Sandbox

test_timeout_sec = 3600
logger = init_logger(__name__)

async def run_swe_evaluation(sandbox: Sandbox, task_dir: Path, instruction: str, agent_config_path: str) -> bool:
    """Run SWE evaluation on the sandbox."""
    task_name = task_dir.name
    # 1. Install agent
    await sandbox.agent.install(config=agent_config_path)

    # 2. Run agent to resolve the task
    result = await sandbox.agent.run(instruction)
    logger.info(f"Task name: {task_name}, sandbox id: {sandbox.sandbox_id}, Agent run result: {result}")

    # 3. Setup test env
    session = "swe-evaluation"
    await sandbox.create_session(
        CreateBashSessionRequest(
            session=session,
            env_enable=True,
            env={"UV_PYTHON_INSTALL_MIRROR": "https://registry.npmmirror.com/-/binary/python-build-standalone"},
        )
    )

    test_file_dir = task_dir / "tests"
    sandbox_test_dir = "/tests"
    is_success = await setup_test_env(
        sandbox, test_file_dir, sandbox_test_dir, task_dir / "run-tests.sh", session=session
    )
    if not is_success:
        logger.error("Failed to setup test environment")
        return False

    # 4. Run tests
    logger.info(f"Task name: {task_name}, sandbox id: {sandbox.sandbox_id}, Start to run tests")
    run_tests_command = f"sh -c 'bash {sandbox_test_dir}/run-tests.sh'"
    resp: Observation = await sandbox.arun(
        run_tests_command, session=session, mode=RunMode.NOHUP, wait_timeout=test_timeout_sec
    )
    logger.info(f"Task name: {task_name}, sandbox id: {sandbox.sandbox_id}, Run tests result: {resp}")

    # 5. Parse results
    resolve_result = parse_swebench_result(resp.output)
    logger.info(f"Task name: {task_name}, sandbox id: {sandbox.sandbox_id}, is_resolved: {resolve_result}")
    return resolve_result


async def run_task(task_dir: Path, agent_config_path: str) -> dict:
    """Run evaluation for a single task."""
    task_name = task_dir.name

    try:
        # Load task configuration
        task_config = load_task_config(task_dir)
        instruction = task_config.get("instruction", "")

        if not instruction:
            logger.error(f"No instruction found in task.yaml for {task_name}")
            return {"task_name": task_name, "status": "failed", "error": "No instruction in task.yaml"}

        # Start sandbox
        sandbox = await start_sandbox(task_name)

        try:
            # Run evaluation
            resolve_result = await run_swe_evaluation(sandbox, task_dir, instruction, agent_config_path)
            logger.info(f"Completed evaluation for task: {task_name}")
            return {
                "task_name": task_name,
                "sandbox_id": sandbox.sandbox_id,
                "status": "success",
                "resolved": resolve_result,
            }
        except Exception as e:
            logger.error(f"Error running evaluation for {task_name}: {e}")
            return {"task_name": task_name, "sandbox_id": sandbox.sandbox_id, "status": "failed", "error": str(e)}
        finally:
            await sandbox.stop()
    except Exception as e:
        logger.error(f"Error loading task config for {task_name}: {e}")
        return {"task_name": task_name, "status": "failed", "error": str(e)}


if __name__ == "__main__":
    cur_dir = Path(__file__).resolve().parent
    agent_config_path = f"{cur_dir}/iflow_config.yaml"

    # directory containing tasks, from https://github.com/laude-institute/terminal-bench-datasets/tree/main/datasets/swebench-verified
    tasks_folder = "path_to_swebench_verified_tasks"  # e.g., "/path/to/swebench-verified-tasks"
    task_name = "task_name"  # task name to run, e.g., "astropy__astropy-12907"

    task_dir = Path(tasks_folder) / task_name
    if not task_dir.exists():
        logger.error(f"Task {task_name} not found")
        sys.exit(1)

    # Ensure admin server is running before executing
    asyncio.run(run_task(task_dir, agent_config_path))
