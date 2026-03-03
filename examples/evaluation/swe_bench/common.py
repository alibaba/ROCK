import re
import yaml
from pathlib import Path

from rock.logger import init_logger
from rock.sdk.sandbox.client import RunMode, Sandbox
from rock.sdk.sandbox.config import SandboxConfig

UV_VERSION = "0.10.5"
UV_ARCH = "x86_64-unknown-linux-gnu"

SWEBENCH_RESULT_START_MARKER = "SWEBench results starts here"
SWEBENCH_RESULT_END_MARKER = "SWEBench results ends here"
SWEBENCH_PASSED = "PASSED"

logger = init_logger(__name__)

def load_task_config(task_dir: Path) -> dict:
    """Load task configuration from task.yaml."""
    task_yaml_path = task_dir / "task.yaml"
    if not task_yaml_path.exists():
        raise FileNotFoundError(f"task.yaml not found in {task_dir}")

    with open(task_yaml_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    return config


async def _install_uv(sandbox: Sandbox, session: str):
    uv_install_script_commands = [
        f"wget https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{UV_ARCH}.tar.gz",
        f"tar -xzf uv-{UV_ARCH}.tar.gz --strip-components=1 -C /usr/local/bin",
        "uv --version",  # verify installation
    ]
    for cmd in uv_install_script_commands:
        result = await sandbox.arun(cmd, session=session, mode=RunMode.NOHUP)
        if result.exit_code != 0:
            raise RuntimeError(f"Failed to install uv: {cmd}, output: {result.output}")


async def setup_test_env(
    sandbox: Sandbox, test_folder: Path, test_dir: str, run_tests_scripts: Path, session: str
) -> bool:
    try:
        await _install_uv(sandbox, session)

        res = await sandbox.fs.upload_dir(test_folder, test_dir)
        if res.exit_code != 0:
            return False

        res = await sandbox.upload_by_path(run_tests_scripts, f"{test_dir}/{run_tests_scripts.name}")
        if not res.success:
            return False

        return True
    except Exception as e:
        logger.error(f"Failed to setup test environment: {e}")
        raise e


def parse_swebench_result(output: str) -> bool:
    """Parse SWEBench test output to determine if the task is resolved.

    Matches the block between 'SWEBench results starts here' and
    'SWEBench results ends here', then checks whether it contains 'PASSED'.
    """
    pattern = rf"{SWEBENCH_RESULT_START_MARKER}\s*(.*?)\s*{SWEBENCH_RESULT_END_MARKER}"
    match = re.search(pattern, output, re.DOTALL)
    if not match:
        return False
    return match.group(1).strip() == SWEBENCH_PASSED


async def start_sandbox(swe_task_name: str) -> Sandbox:
    """Start a sandbox instance for evaluation."""
    image = f"slimshetty/swebench-verified:sweb.eval.x86_64.{swe_task_name}"
    config = SandboxConfig(image=image)
    sandbox = Sandbox(config)
    await sandbox.start()
    return sandbox
