"""APT speedup strategy implementation."""

import logging

from rock.actions import Command
from rock.sdk.sandbox.speedup.base import SpeedupStrategy
from rock.sdk.sandbox.speedup.constants import setup_apt_source_template

logger = logging.getLogger(__name__)


class AptSpeedupStrategy(SpeedupStrategy):
    """APT speedup strategy"""

    async def precheck(self, sandbox) -> tuple[bool, str]:
        """Check if the system is Debian/Ubuntu based"""
        try:
            # Use execute instead of arun for simple checks
            result = await sandbox.execute(Command(command=["test", "-f", "/etc/debian_version"]))
            if result.exit_code == 0:
                logger.info("APT precheck passed: Debian/Ubuntu system detected")
                return True, "System check passed: Debian/Ubuntu detected"
            else:
                logger.warning("APT precheck failed: Not a Debian/Ubuntu system")
                return False, "This is not a Debian/Ubuntu system, APT speedup is not supported"
        except Exception as e:
            logger.error(f"APT precheck failed with exception: {e}")
            return False, f"System check failed: {str(e)}"

    def parse_value(self, speedup_value: str) -> dict[str, str]:
        """
        Parse APT mirror URL

        Args:
            speedup_value: Mirror URL with protocol

        Examples:
            http://mirrors.cloud.aliyuncs.com -> {
                "mirror_base": "http://mirrors.cloud.aliyuncs.com"
            }
            https://mirrors.aliyun.com/ -> {
                "mirror_base": "https://mirrors.aliyun.com"
            }
        """
        # Remove trailing slash
        mirror_base = speedup_value.rstrip("/")

        return {"mirror_base": mirror_base}

    def generate_script(self, speedup_value: str) -> str:
        """Generate APT speedup script"""
        params = self.parse_value(speedup_value)
        logger.info(f"Generating APT speedup script with mirror: {params['mirror_base']}")
        return setup_apt_source_template.format(**params)
