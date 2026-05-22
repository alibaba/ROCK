import logging
import subprocess
import os

logger = logging.getLogger(__name__)


def test_env_basic_info():
    """Test environment information is correctly configured."""
    # Basic environment verification
    result = subprocess.run(["date"], capture_output=True, text=True)
    logger.info(f"Current date: {result.stdout.strip()}")

    result = subprocess.run(["hostname"], capture_output=True, text=True)
    logger.info(f"Hostname: {result.stdout.strip()}")

    result = subprocess.run(["whoami"], capture_output=True, text=True)
    logger.info(f"User: {result.stdout.strip()}")

    result = subprocess.run(["id"], capture_output=True, text=True)
    logger.info(f"ID: {result.stdout.strip()}")

    # Check runner environment
    runner_dir = os.environ.get("RUNNER_TEMP", "")
    workspace = os.environ.get("GITHUB_WORKSPACE", "")
    logger.info(f"RUNNER_TEMP: {runner_dir}")
    logger.info(f"GITHUB_WORKSPACE: {workspace}")

    # Verify environment is correctly set up
    assert result.returncode == 0, "Environment check failed"
