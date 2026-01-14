"""Abstract base class for speedup strategies."""

from abc import ABC, abstractmethod


class SpeedupStrategy(ABC):
    """Speedup strategy abstract base class"""

    @abstractmethod
    async def precheck(self, sandbox) -> tuple[bool, str]:
        """
        Precheck if environment meets requirements

        Args:
            sandbox: Sandbox instance

        Returns:
            tuple[bool, str]: (check passed, check message)
        """
        pass

    @abstractmethod
    def generate_script(self, speedup_value: str) -> str:
        """
        Generate speedup configuration script

        Args:
            speedup_value: Speedup value (mirror URL, IP address, etc.)

        Returns:
            str: Script content
        """
        pass

    @abstractmethod
    def parse_value(self, speedup_value: str) -> dict[str, str]:
        """
        Parse speedup value and extract required parameters

        Args:
            speedup_value: Speedup value string

        Returns:
            dict[str, str]: Parameters for template filling
        """
        pass

    def get_nohup_wait_timeout(self) -> int:
        """
        Get nohup wait timeout (seconds)

        Returns:
            int: Timeout value
        """
        return 30
