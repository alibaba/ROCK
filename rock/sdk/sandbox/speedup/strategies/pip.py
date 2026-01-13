"""PIP speedup strategy implementation."""

import logging
from urllib.parse import urlparse

from rock.actions import Command
from rock.sdk.sandbox.speedup.base import SpeedupStrategy
from rock.sdk.sandbox.speedup.constants import setup_pip_source_template

logger = logging.getLogger(__name__)


class PipSpeedupStrategy(SpeedupStrategy):
    """PIP speedup strategy"""

    async def precheck(self, sandbox) -> tuple[bool, str]:
        """Check if pip is installed"""
        try:
            # Try pip3 first, then pip
            result = await sandbox.execute(Command(command=["sh", "-c", "pip3 --version 2>&1 || pip --version 2>&1"]))
            if result.exit_code == 0:
                pip_version = result.stdout.strip()
                logger.info(f"PIP precheck passed: {pip_version}")
                return True, f"PIP check passed: {pip_version}"
            else:
                logger.warning("PIP precheck failed: pip not found")
                return False, "pip is not installed, PIP speedup is not supported"
        except Exception as e:
            logger.error(f"PIP precheck failed with exception: {e}")
            return False, f"PIP check failed: {str(e)}"

    def parse_value(self, speedup_value: str) -> dict[str, str]:
        """
        Parse PIP mirror URL

        Args:
            speedup_value: Mirror URL with protocol

        Examples:
            http://mirrors.cloud.aliyuncs.com -> {
                "pip_index_url": "http://mirrors.cloud.aliyuncs.com/pypi/simple/",
                "pip_trusted_host": "mirrors.cloud.aliyuncs.com"
            }
            https://mirrors.aliyun.com -> {
                "pip_index_url": "https://mirrors.aliyun.com/pypi/simple/",
                "pip_trusted_host": "mirrors.aliyun.com"
            }
        """
        # Remove trailing slash
        base_url = speedup_value.rstrip("/")

        # Extract trusted host from URL
        parsed = urlparse(base_url)
        trusted_host = parsed.netloc

        # Build index URL by appending /pypi/simple/
        index_url = f"{base_url}/pypi/simple/"

        return {"pip_index_url": index_url, "pip_trusted_host": trusted_host}

    def generate_script(self, speedup_value: str) -> str:
        """Generate PIP speedup script"""
        params = self.parse_value(speedup_value)
        logger.info(f"Generating PIP speedup script with mirror: {params['pip_index_url']}")
        return setup_pip_source_template.format(**params)
