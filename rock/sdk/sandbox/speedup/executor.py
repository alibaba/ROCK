"""Speedup executor for coordinating speedup operations."""

from __future__ import annotations  # Postpone annotation evaluation to avoid circular imports.

import logging
from typing import TYPE_CHECKING

from rock.actions import Observation
from rock.sdk.sandbox.speedup.base import SpeedupStrategy
from rock.sdk.sandbox.speedup.strategies.apt import AptSpeedupStrategy
from rock.sdk.sandbox.speedup.strategies.github import GithubSpeedupStrategy
from rock.sdk.sandbox.speedup.strategies.pip import PipSpeedupStrategy
from rock.sdk.sandbox.speedup.types import SpeedupType

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = logging.getLogger(__name__)


class SpeedupExecutor:
    """Speedup executor (coordinator)"""

    # Strategy registry
    _strategies: dict[SpeedupType, type[SpeedupStrategy]] = {
        SpeedupType.APT: AptSpeedupStrategy,
        SpeedupType.PIP: PipSpeedupStrategy,
        SpeedupType.GITHUB: GithubSpeedupStrategy,
    }

    def __init__(self, sandbox: Sandbox):
        """
        Initialize executor

        Args:
            sandbox: Sandbox instance
        """
        self.sandbox = sandbox

    @classmethod
    def register_strategy(cls, speedup_type: SpeedupType, strategy_class: type[SpeedupStrategy]):
        """
        Register a new speedup strategy

        Args:
            speedup_type: Speedup type
            strategy_class: Strategy class
        """
        cls._strategies[speedup_type] = strategy_class
        logger.info(f"Registered speedup strategy: {speedup_type} -> {strategy_class.__name__}")

    async def execute(self, speedup_type: SpeedupType, speedup_value: str, timeout: int = 300) -> Observation:
        """
        Execute speedup configuration (template method pattern)

        Args:
            speedup_type: Speedup type (APT, PIP, GITHUB, etc.)
            speedup_value: Speedup value string (mirror URL, IP address, etc.)
            timeout: Timeout in seconds

        Returns:
            Observation: Execution result
        """
        logger.info(f"Starting speedup: type={speedup_type}, value={speedup_value}, timeout={timeout}")

        # 1. Get strategy
        strategy = self._get_strategy(speedup_type)
        if not strategy:
            error_msg = f"Unsupported speedup type: {speedup_type}"
            logger.error(error_msg)
            return Observation(output=error_msg, exit_code=1, failure_reason="Invalid speedup type")

        # 2. Precheck environment
        precheck_success, precheck_msg = await self._precheck(strategy)
        if not precheck_success:
            logger.warning(f"Precheck failed: {precheck_msg}")
            return Observation(output=precheck_msg, exit_code=1, failure_reason="Precheck failed")

        logger.info(f"Precheck passed: {precheck_msg}")

        # 3. Generate script
        script_content = self._generate_script(strategy, speedup_value)
        if not script_content:
            error_msg = "Failed to generate speedup script"
            logger.error(error_msg)
            return Observation(output=error_msg, exit_code=1, failure_reason="Script generation failed")

        # 4. Execute script using the general execute_script method
        result = await self.sandbox.process.execute_script(
            script_content=script_content,
            wait_timeout=strategy.get_nohup_wait_timeout(),
            cleanup=True,
        )

        # 5. Log result
        if result.exit_code == 0:
            logger.info(f"Speedup completed successfully: type={speedup_type}, output_length={len(result.output)}")
        else:
            logger.error(
                f"Speedup failed: type={speedup_type}, exit_code={result.exit_code}, "
                f"failure_reason={result.failure_reason}"
            )

        return result

    def _get_strategy(self, speedup_type: SpeedupType) -> SpeedupStrategy | None:
        """Get strategy instance"""
        strategy_class = self._strategies.get(speedup_type)
        if not strategy_class:
            return None
        return strategy_class()

    async def _precheck(self, strategy: SpeedupStrategy) -> tuple[bool, str]:
        """Execute precheck"""
        try:
            logger.debug("Running precheck...")
            return await strategy.precheck(self.sandbox)
        except Exception as e:
            logger.error(f"Precheck exception: {e}")
            return False, f"Precheck failed with exception: {str(e)}"

    def _generate_script(self, strategy: SpeedupStrategy, speedup_value: str) -> str | None:
        """Generate script content"""
        try:
            logger.debug(f"Generating script for value: {speedup_value}")
            return strategy.generate_script(speedup_value)
        except Exception as e:
            logger.error(f"Script generation exception: {e}")
            return None
