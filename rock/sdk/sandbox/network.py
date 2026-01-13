from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

from typing import TYPE_CHECKING

from rock.actions import Observation
from rock.logger import init_logger
from rock.sdk.sandbox.speedup.executor import SpeedupExecutor
from rock.sdk.sandbox.speedup.types import SpeedupType

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)


class Network:
    """Network management for sandbox"""

    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox
        self._speedup_executor = SpeedupExecutor(sandbox)

    async def speedup(self, speedup_type: SpeedupType, speedup_value: str, timeout: int = 300) -> Observation:
        """
        Configure acceleration for package managers or network resources

        Args:
            speedup_type: Type of speedup configuration (SpeedupType.APT, SpeedupType.PIP, SpeedupType.GITHUB)
            speedup_value: Speedup value, format depends on speedup_type:
                - APT: Mirror URL with protocol
                    Examples: "http://mirrors.cloud.aliyuncs.com", "https://mirrors.aliyun.com"
                - PIP: Mirror URL with protocol
                    Examples: "http://mirrors.cloud.aliyuncs.com", "https://mirrors.aliyun.com"
                - GITHUB: IP address for github.com
                    Examples: "11.11.11.11"
            timeout: Execution timeout in seconds, default 300

        Returns:
            Observation: Execution result containing output and exit code

        Examples:
            # Configure APT mirror
            result = await sandbox.network.speedup(
                speedup_type=SpeedupType.APT,
                speedup_value="http://mirrors.cloud.aliyuncs.com"
            )

            # Configure PIP mirror with custom path
            result = await sandbox.network.speedup(
                speedup_type=SpeedupType.PIP,
                speedup_value="https://mirrors.aliyun.com"
            )

            # Configure GitHub acceleration
            result = await sandbox.network.speedup(
                speedup_type=SpeedupType.GITHUB,
                speedup_value="11.11.11.11"
            )
        """
        return await self._speedup_executor.execute(speedup_type, speedup_value, timeout)
