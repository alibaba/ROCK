from __future__ import annotations

import os
import shlex
import uuid
from string import Template
from typing import TYPE_CHECKING

from rock.logger import init_logger

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class Deploy:
    """Sandbox 资源部署管理器.

    提供:
    - deploy_working_dir(): 将本地目录部署到 sandbox
    - format(): 使用 ${working_dir} 模板替换
    """

    def __init__(self, sandbox: Sandbox):
        self._sandbox = sandbox
        self._working_dir: str | None = None

    @property
    def working_dir(self) -> str | None:
        """返回当前部署的 working_dir 在 sandbox 中的路径."""
        return self._working_dir

    async def deploy_working_dir(
        self,
        local_path: str,
        target_path: str | None = None,
    ) -> str:
        """将本地目录部署到 sandbox.

        支持多次调用，后面的调用会覆盖之前的路径。

        Args:
            local_path: 本地目录路径（相对或绝对）
            target_path: sandbox 目标路径（默认: /tmp/rock_workdir_<uuid>）

        Returns:
            sandbox 中的目标路径
        """
        local_abs = os.path.abspath(local_path)

        # 验证本地路径
        if not os.path.exists(local_abs):
            raise FileNotFoundError(f"local_path not found: {local_abs}")
        if not os.path.isdir(local_abs):
            raise ValueError(f"local_path must be a directory: {local_abs}")

        # 确定目标路径
        if target_path is None:
            target_path = f"/tmp/rock_workdir_{uuid.uuid4().hex}"

        sandbox_id = self._sandbox.sandbox_id
        logger.info(f"[{sandbox_id}] Deploying working_dir: {local_abs} -> {target_path}")

        # 创建目标目录
        result = await self._sandbox.execute(
            cmd=["bash", "-c", f"rm -rf {shlex.quote(target_path)} && mkdir -p {shlex.quote(target_path)}"],
        )
        if result.exit_code != 0:
            raise RuntimeError(f"Failed to create target directory: {result.output}")

        # 上传目录
        upload_result = await self._sandbox.fs.upload_dir(source_dir=local_abs, target_dir=target_path)
        if upload_result.exit_code != 0:
            raise RuntimeError(f"Failed to upload directory: {upload_result.failure_reason}")

        # 覆盖之前的 working_dir
        self._working_dir = target_path
        logger.info(f"[{sandbox_id}] working_dir deployed: {target_path}")
        return target_path

    def format(self, template: str, **kwargs: str) -> str:
        """使用 string.Template 格式化命令模板.

        Args:
            template: 包含 ${working_dir} 等占位符的命令模板
            **kwargs: 其他变量替换，如 prompt="xxx" 替换 ${prompt}

        Returns:
            格式化后的命令

        Raises:
            RuntimeError: 如果没有部署过 working_dir

        Example:
            >>> deploy.format("mv ${working_dir}/config.json /root/.app/")
            "mv /tmp/rock_workdir_abc123/config.json /root/.app/"

            >>> deploy.format("cat ${working_dir}/${prompt}", prompt="test.txt")
            "cat /tmp/rock_workdir_abc123/test.txt"
        """
        if self._working_dir is None:
            raise RuntimeError("No working_dir deployed yet. Call deploy_working_dir() first.")

        substitutions = {"working_dir": self._working_dir, **kwargs}
        return Template(template).substitute(**substitutions)
