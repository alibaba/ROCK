"""GitHub speedup strategy implementation."""

import logging
import re

from rock.actions import Command
from rock.sdk.sandbox.speedup.base import SpeedupStrategy
from rock.sdk.sandbox.speedup.constants import setup_github_hosts_template

logger = logging.getLogger(__name__)


class GithubSpeedupStrategy(SpeedupStrategy):
    """GitHub speedup strategy for github.com acceleration"""

    async def precheck(self, sandbox) -> tuple[bool, str]:
        """Check if /etc/hosts is writable"""
        try:
            # Check if /etc/hosts exists and is writable
            result = await sandbox.execute(Command(command=["test", "-w", "/etc/hosts"]))
            if result.exit_code == 0:
                logger.info("GitHub precheck passed: /etc/hosts is writable")
                return True, "System check passed: /etc/hosts is writable"
            else:
                logger.warning("GitHub precheck failed: /etc/hosts is not writable")
                return False, "/etc/hosts is not writable, GitHub speedup requires root privileges"
        except Exception as e:
            logger.error(f"GitHub precheck failed with exception: {e}")
            return False, f"System check failed: {str(e)}"

    def parse_value(self, speedup_value: str) -> dict[str, str]:
        """
        Parse GitHub IP address for github.com acceleration

        Args:
            speedup_value: IP address for github.com

        Examples:
            "11.11.11.11" -> {
                "hosts_entry": "11.11.11.11 github.com"
            }
        """
        # Trim whitespace
        ip_address = speedup_value.strip()

        # Validate IP address format
        ip_pattern = r"^(\d{1,3}\.){3}\d{1,3}$"
        if not re.match(ip_pattern, ip_address):
            logger.warning(f"Invalid IP address format: {ip_address}")
            raise ValueError(f"Invalid IP address format: {ip_address}. Expected format: x.x.x.x")

        # Validate IP address range (0-255 for each octet)
        octets = ip_address.split(".")
        for octet in octets:
            if int(octet) > 255:
                logger.warning(f"Invalid IP address: {ip_address}, octet value exceeds 255")
                raise ValueError(f"Invalid IP address: {ip_address}, octet value must be 0-255")

        # Build hosts entry for github.com
        hosts_entry = f"{ip_address} github.com"

        return {"hosts_entry": hosts_entry}

    def generate_script(self, speedup_value: str) -> str:
        """Generate GitHub hosts speedup script"""
        params = self.parse_value(speedup_value)
        logger.info(f"Generating GitHub speedup script with hosts entry: {params['hosts_entry']}")
        return setup_github_hosts_template.format(**params)
