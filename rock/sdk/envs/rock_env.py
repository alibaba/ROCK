from typing import Any

import httpx

from rock import env_vars
from rock.actions import Env
from rock.logger import init_logger

logger = init_logger(__name__)

class RockEnv(Env):
    def __init__(self, env_id: str) -> None:
        """
        Initialize the environment.

        Args:
            env_id: Environment ID.

        Raises:
            Exception: Raised when environment initialization fails.
        """
        self._env_id = env_id
        self._sandbox_id: str | None = None
        self._is_closed = False
        try:
            self._initialize_environment()
        except Exception as e:
            raise Exception(f"Failed to initialize environment: {e}") from e

    def _initialize_environment(self) -> None:
        """Initialize environment instance."""
        result = self._call_admin_api("make", {"env_id": self._env_id})
        self._sandbox_id = result.get("sandbox_id")
        if not self._sandbox_id:
            raise Exception("Failed to get environment instance ID")

    def step(self, action: str) -> tuple[str, float, bool, bool, dict[str, Any]]:
        """
        Execute an action step.

        Args:
            action: Action ID to execute (0-3).

        Returns:
            Tuple containing observation, reward, termination status, truncation status, and info.

        Raises:
            Exception: Raised when action is invalid or environment is closed.
        """
        params = {
            "sandbox_id": self._sandbox_id,
            "action": action,
        }
        try:
            result = self._call_admin_api("step", params)
            return self._parse_step_result(result)
        except Exception as e:
            raise Exception(f"Failed to execute step with action {action}: {e}") from e

    def reset(self, seed: int | None = None) -> tuple[str, dict[str, Any]]:
        """
        Reset environment to initial state.

        Args:
            seed: Random seed.

        Returns:
            Tuple containing initial observation and info.

        Raises:
            Exception: Raised when reset fails.
        """
        params = {"sandbox_id": self._sandbox_id}
        if seed is not None:
            params["seed"] = seed
        try:
            result = self._call_admin_api("reset", params)
            return self._parse_reset_result(result)
        except Exception as e:
            raise Exception(f"Failed to reset environment: {e}") from e

    def close(self) -> None:
        """
        Close environment and clean up resources.

        Raises:
            Exception: Raised when closing environment fails.
        """
        if self._is_closed or not self._sandbox_id:
            return
        try:
            self._call_admin_api("close", {"sandbox_id": self._sandbox_id})
        except Exception as e:
            raise Exception(f"Failed to close environment: {e}") from e
        finally:
            self._is_closed = True
            self._sandbox_id = None

    def _parse_step_result(self, result: dict[str, Any]) -> tuple[Any, float, bool, bool, dict[str, Any]]:
        """Parse the result of step operation."""
        try:
            return (
                result["observation"],
                result["reward"],
                result["terminated"],
                result["truncated"],
                result["info"],
            )
        except (KeyError, TypeError) as e:
            raise Exception(f"Invalid step result format: {e}. Result: {result}") from e

    def _parse_reset_result(self, result: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """Parse the result of reset operation."""
        try:
            return result["observation"], result["info"]
        except (KeyError, TypeError) as e:
            raise Exception(f"Invalid reset result format: {e}. Result: {result}") from e

    def _call_admin_api(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Helper method to call Admin API.

        Args:
            endpoint: API endpoint name.
            params: Request parameters.

        Returns:
            Dictionary of API response results.

        Raises:
            Exception: Raised when API call fails.
        """
        url = f"{env_vars.ROCK_BASE_URL}/apis/v1/envs/gem/{endpoint}"
        headers = {"Content-Type": "application/json"}
        # Unified timeout configuration
        timeout = httpx.Timeout(timeout=300.0, connect=300.0, read=300.0)
        try:
            logger.debug(f"Calling Admin API {url} with params: {params}")
            
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, headers=headers, json=params)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            raise Exception(f"HTTP error {e.response.status_code} for {endpoint}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise Exception(f"Request error for {url}: {e}") from e
        except ValueError as e:
            raise Exception(f"JSON decode error for {url}: {e}") from e
        except Exception as e:
            raise Exception(f"Unexpected error calling {url}: {e}") from e
