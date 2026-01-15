from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import shlex
import tarfile
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from rock.actions import Command, CreateBashSessionRequest, Observation
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

    async def upload_dir(
        self,
        source_dir: str | Path,
        target_dir: str,
        extract_timeout: int = 600,
    ) -> Observation:
        """Upload local directory to sandbox using tar.gz (simple version).

        - Check 'tar' exists; if not, return Observation with exit_code != 0
        - Pack source_dir fully into a tar.gz locally (no symlink filtering)
        - Upload to sandbox /tmp
        - Create a random bash session internally
        - Extract into target_dir
        - Always cleanup local tar.gz

        Returns:
            Observation(exit_code=0) on success, otherwise exit_code!=0 with failure_reason.
        """
        local_tar_path: Path | None = None
        remote_tar_path: str | None = None
        session: str | None = None

        try:
            src = Path(source_dir).expanduser().resolve()
            if not src.exists():
                return Observation(exit_code=1, failure_reason=f"source_dir not found: {src}")
            if not src.is_dir():
                return Observation(exit_code=1, failure_reason=f"source_dir must be a directory: {src}")
            if not isinstance(target_dir, str) or not target_dir.startswith("/"):
                return Observation(exit_code=1, failure_reason=f"target_dir must be absolute path: {target_dir}")

            ts = str(time.time_ns())
            local_tar_path = Path(tempfile.gettempdir()) / f"rock_upload_{ts}.tar.gz"
            remote_tar_path = f"/tmp/rock_upload_{ts}.tar.gz"
            session = f"bash-{ts}"

            # create bash session
            await self.sandbox.create_session(CreateBashSessionRequest(session=session))

            # check tar exists
            check = await self.sandbox.arun(
                cmd="command -v tar >/dev/null 2>&1",
                session=session,
            )
            if check.exit_code != 0:
                return Observation(exit_code=1, failure_reason="sandbox has no tar command; cannot extract tarball")

            # pack locally
            try:
                with tarfile.open(local_tar_path, "w:gz") as tf:
                    tf.add(str(src), arcname=".")
            except Exception as e:
                raise Exception(f"tar pack failed: {e}")

            # upload tarball
            upload_response = await self.sandbox.upload_by_path(
                file_path=str(local_tar_path), target_path=remote_tar_path
            )
            if not upload_response.success:
                return Observation(exit_code=1, failure_reason=f"tar upload failed: {upload_response.message}")

            # extract
            extract_cmd = (
                f"rm -rf {shlex.quote(target_dir)} && mkdir -p {shlex.quote(target_dir)} && "
                f"tar -xzf {shlex.quote(remote_tar_path)} -C {shlex.quote(target_dir)}"
            )
            from rock.sdk.sandbox.client import RunMode

            res = await self.sandbox.arun(
                cmd=f"bash -c {shlex.quote(extract_cmd)}",
                mode=RunMode.NOHUP,
                wait_timeout=extract_timeout,
            )
            if res.exit_code != 0:
                return Observation(exit_code=1, failure_reason=f"tar extract failed: {res.output}")

            # cleanup remote tarball
            try:
                await self.sandbox.execute(Command(command=["rm", "-f", remote_tar_path]))
            except Exception:
                pass

            return Observation(exit_code=0, output=f"uploaded {src} -> {target_dir} via tar")

        except Exception as e:
            return Observation(exit_code=1, failure_reason=f"upload_dir unexpected error: {e}")

        finally:
            # cleanup local tarball
            try:
                if local_tar_path and local_tar_path.exists():
                    local_tar_path.unlink()
            except Exception:
                pass
