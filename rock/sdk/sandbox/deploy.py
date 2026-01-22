from __future__ import annotations

import os
import uuid
from string import Template
from typing import TYPE_CHECKING

from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class Deploy:
    """Sandbox resource deployment manager.

    Provides:
    - deploy_working_dir(): Deploy local directory to sandbox
    - format(): Replace ${working_dir} template placeholders
    """

    def __init__(self, sandbox: Sandbox):
        self._sandbox = sandbox
        self._working_dir: str | None = None

    @property
    def working_dir(self) -> str | None:
        """Returns the working_dir path deployed in the sandbox."""
        return self._working_dir

    async def deploy_working_dir(
        self,
        local_path: str,
        target_path: str | None = None,
    ) -> str:
        """Deploy local directory to sandbox.

        Supports multiple calls; later calls will overwrite previous paths.

        Args:
            local_path: Local directory path (relative or absolute).
            target_path: Target path in sandbox (default: /tmp/rock_workdir_<uuid>).

        Returns:
            The target path in sandbox.
        """
        local_abs = os.path.abspath(local_path)

        # Validate local path
        if not os.path.exists(local_abs):
            raise FileNotFoundError(f"local_path not found: {local_abs}")
        if not os.path.isdir(local_abs):
            raise ValueError(f"local_path must be a directory: {local_abs}")

        # Determine target path
        if target_path is None:
            target_path = f"/tmp/rock_workdir_{uuid.uuid4().hex}"

        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Deploying working_dir: {local_abs} -> {target_path}")

        # Upload directory
        upload_result = await self._sandbox.fs.upload_dir(source_dir=local_abs, target_dir=target_path)
        if upload_result.exit_code != 0:
            raise RuntimeError(f"Failed to upload directory: {upload_result.failure_reason}")

        # Overwrite previous working_dir
        self._working_dir = target_path
        logger.info(f"[{sandbox_id}] working_dir deployed: {target_path}")
        return target_path

    def format(self, template: str, **kwargs: str) -> str:
        """Format command template using string.Template.

        Args:
            template: Command template containing placeholders like ${working_dir}.
            **kwargs: Additional variable replacements, e.g., prompt="xxx" replaces ${prompt}.

        Returns:
            The formatted command.

        Raises:
            RuntimeError: If working_dir has not been deployed.

        Example:
            >>> deploy.format("mv ${working_dir}/config.json /root/.app/")
            "mv /tmp/rock_workdir_abc123/config.json /root/.app/"

            >>> deploy.format("cat ${working_dir}/${prompt}", prompt="test.txt")
            "cat /tmp/rock_workdir_abc123/test.txt"
        """
        subs = {
            **kwargs,
            **({"working_dir": self._working_dir} if self._working_dir else {}),
        }
        subs = {k: v for k, v in subs.items() if v is not None}

        return Template(template).safe_substitute(subs)
