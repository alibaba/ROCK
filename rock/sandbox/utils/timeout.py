"""Sandbox timeout helpers.

Pure calculation utilities — no I/O.  Callers are responsible for reading
timeout_info from SandboxMetaStore and writing the result back.
"""

from __future__ import annotations

import time

from rock import env_vars


class SandboxTimeoutHelper:
    """Stateless timeout calculation helpers."""

    @staticmethod
    def make_timeout_info(auto_clear_time: int) -> dict[str, str]:
        """Build the initial timeout dict for a newly created sandbox.

        Parameters
        ----------
        auto_clear_time:
            Sandbox lifetime in **minutes**.
        """
        expire_time = int(time.time()) + auto_clear_time * 60
        return {
            env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: str(auto_clear_time),
            env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: str(expire_time),
        }

    @staticmethod
    def refresh_timeout(timeout_info: dict[str, str]) -> dict[str, str] | None:
        """Return a new timeout dict with ``expire_time`` recalculated from ``auto_clear_time``.

        Returns *None* if ``auto_clear_time`` is missing from *timeout_info*.
        """
        auto_clear_time = timeout_info.get(env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY)
        if auto_clear_time is None:
            return None
        expire_time = int(time.time()) + int(auto_clear_time) * 60
        return {
            env_vars.ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY: str(auto_clear_time),
            env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY: str(expire_time),
        }

    @staticmethod
    def is_expired(timeout_info: dict[str, str]) -> bool:
        """Return *True* when ``expire_time`` in *timeout_info* is in the past."""
        expire_time = int(timeout_info.get(env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY, 0))
        return int(time.time()) > expire_time
