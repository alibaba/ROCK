from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

from typing import TYPE_CHECKING

from rock.utils import retry_async

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox


@retry_async(max_attempts=3, delay_seconds=5.0, backoff=2.0)
async def arun_with_retry(
    sandbox: Sandbox,
    cmd: str,
    session: str,
    mode: str = "nohup",
    wait_timeout: int = 300,
    wait_interval: int = 10,
    error_msg: str = "Command failed",
):
    result = await sandbox.arun(
        cmd=cmd, session=session, mode=mode, wait_timeout=wait_timeout, wait_interval=wait_interval
    )
    # If exit_code is not 0, raise an exception to trigger retry
    if result.exit_code != 0:
        raise Exception(f"{error_msg} with exit code: {result.exit_code}, output: {result.output}")
    return result
