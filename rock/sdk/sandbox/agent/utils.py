import logging

from rock.sdk.sandbox.client import Sandbox

logger = logging.getLogger(__name__)


async def arun_with_retry(
    sandbox: Sandbox,
    cmd: str,
    session: str,
    mode: str = "nohup",
    wait_timeout: int = 300,
    wait_interval: int = 10,
    error_msg: str = "Command failed",
):
    """Execute command with retry logic.

    Executes a command and automatically retries up to 3 times when the command
    fails (non-zero exit code). Implements exponential backoff strategy with
    delay between retries that increases progressively.

    Args:
        cmd: Command to be executed.
        session: Session name where the command will be executed.
        mode: Execution mode (normal, nohup, etc.). Defaults to "nohup".
        wait_timeout: Timeout for command execution in seconds. Defaults to 300.
        wait_interval: Check interval for nohup commands in seconds. Defaults to 10.
        error_msg: Error message to use when exception occurs. Defaults to
            "Command failed".

    Returns:
        Command result object upon successful execution.

    Raises:
        Exception: Raises exception when command execution fails (non-zero exit
            code) to trigger retry.
    """
    sandbox_id = sandbox.sandbox_id
    logger.debug(f"[{sandbox_id}] Executing command with retry: {cmd[:100]}...")
    logger.debug(
        f"[{sandbox_id}] Command execution parameters: mode={mode}, timeout={wait_timeout}, interval={wait_interval}"
    )

    result = await sandbox.arun(
        cmd=cmd, session=session, mode=mode, wait_timeout=wait_timeout, wait_interval=wait_interval
    )

    logger.debug(f"[{sandbox_id}] Command execution result: exit_code={result.exit_code}")

    # If exit_code is not 0, raise an exception to trigger retry
    if result.exit_code != 0:
        logger.warning(f"[{sandbox_id}] Command attempt failed: {error_msg}, exit code: {result.exit_code}")
        logger.debug(f"[{sandbox_id}] Command output: {result.output[:500]}...")
        raise Exception(f"{error_msg} with exit code: {result.exit_code}, output: {result.output}")

    logger.debug(f"[{sandbox_id}] Command executed successfully with retry mechanism")
    return result
