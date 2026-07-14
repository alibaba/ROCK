"""Sandbox timeout helpers.

Pure calculation utilities — no I/O.  Callers are responsible for reading
timeout_info from SandboxMetaStore and writing the result back.
"""

from __future__ import annotations

import time
from typing import Any

from rock import env_vars
from rock.actions.sandbox.response import State
from rock.utils.system import get_iso8601_timestamp


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

    @staticmethod
    def auto_stop_time_from_timeout(timeout_info: dict[str, str] | None) -> str | None:
        if not timeout_info:
            return None
        expire_time = timeout_info.get(env_vars.ROCK_SANDBOX_EXPIRE_TIME_KEY)
        if expire_time is None:
            return None
        try:
            return get_iso8601_timestamp(int(expire_time))
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    @staticmethod
    def auto_transition_times_for_status(
        state: State | str | None,
        sandbox_info: dict[str, Any],
        timeout_info: dict[str, str] | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """Return the single user-facing auto-transition deadline for the current state."""
        normalized_state = state.value if isinstance(state, State) else state

        if normalized_state in (State.PENDING.value, State.RUNNING.value):
            return (
                SandboxTimeoutHelper.auto_stop_time_from_timeout(timeout_info) or sandbox_info.get("auto_stop_time"),
                None,
                None,
            )

        if normalized_state == State.STOPPED.value:
            transition_state = sandbox_info.get("auto_transition_state")
            transition_time = sandbox_info.get("auto_transition_time")
            if transition_state == State.ARCHIVED.value:
                return None, transition_time, None
            if transition_state == State.DELETED.value:
                return None, None, transition_time
            return None, None, None

        if normalized_state == State.ARCHIVED.value:
            if sandbox_info.get("auto_transition_state") == State.DELETED.value:
                return None, None, sandbox_info.get("auto_transition_time")
            return None, None, None

        return None, None, None
