from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import time
from typing import TYPE_CHECKING

from rock.actions import Command, Observation
from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class Process:
    """Process management for sandbox execution"""

    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox

    async def execute_script(
        self,
        script_content: str,
        script_name: str | None = None,
        wait_timeout: int = 300,
        wait_interval: int = 10,
        cleanup: bool = True,
    ) -> Observation:
        """
        Execute a script in the sandbox.

        This is a general-purpose method that:
        1. Uploads the script to /tmp
        2. Executes it using nohup mode
        3. Optionally cleans up the script file

        Args:
            script_content: The script content to execute
            script_name: Optional custom script name. If None, generates timestamp-based name
            wait_timeout: Maximum time to wait for script completion (seconds)
            wait_interval: Interval between process checks (seconds)
            cleanup: Whether to delete the script file after execution

        Returns:
            Observation: Execution result

        Examples:
            # Execute a simple script
            result = await sandbox.process.execute_script(
                script_content="#!/bin/bash\\necho 'Hello World'",
                wait_timeout=60
            )

            # Execute with custom name and keep the script
            result = await sandbox.process.execute_script(
                script_content=my_script,
                script_name="my_custom_script.sh",
                cleanup=False
            )
        """
        from rock.sdk.sandbox.client import Sandbox

        assert isinstance(self.sandbox, Sandbox)

        # Generate script path
        if script_name is None:
            timestamp = str(time.time_ns())
            script_name = f"script_{timestamp}.sh"

        script_path = f"/tmp/{script_name}"

        try:
            # Upload script
            logger.info(f"Uploading script to {script_path}")
            write_result = await self.sandbox.write_file_by_path(script_content, script_path)

            if not write_result.success:
                error_msg = f"Failed to upload script: {write_result.message}"
                logger.error(error_msg)
                return Observation(output=error_msg, exit_code=1, failure_reason="Script upload failed")

            # Execute script
            logger.info(f"Executing script: {script_path} (timeout={wait_timeout}s)")
            result = await self.sandbox.arun(
                cmd=f"bash {script_path}",
                mode="nohup",
                wait_timeout=wait_timeout,
                wait_interval=wait_interval,
            )

            return result

        except Exception as e:
            error_msg = f"Script execution failed: {str(e)}"
            logger.error(error_msg)
            return Observation(output=error_msg, exit_code=1, failure_reason=error_msg)

        finally:
            # Cleanup script if requested
            if cleanup:
                try:
                    logger.info(f"Cleaning up script: {script_path}")
                    await self.sandbox.execute(Command(command=["rm", "-f", script_path]))
                    logger.debug(f"Script cleaned up successfully: {script_path}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup script {script_path}: {e}")
