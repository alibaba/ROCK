"""
Sandbox State Machine using python-statemachine library.

Defines sandbox lifecycle states and transitions. The on_start / on_stop
callbacks contain the actual async business logic so the state machine is
the single place that owns both transition validation and execution.
"""

from statemachine import State as SMState
from statemachine import StateChart

from rock.actions.sandbox.response import State as RockState
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.metrics.billing import log_billing_info
from rock.common.constants import StopReason
from rock.logger import init_logger
from rock.utils.system import get_iso8601_timestamp

logger = init_logger(__name__)


class SandboxStateMachine(StateChart):
    """
    State machine for sandbox lifecycle management.

    States:
        - pending:   Sandbox is being created / starting
        - running:   Sandbox is actively running
        - stopped:   Sandbox has been stopped

    Transitions:
        - stop:      pending/running → stopped  (stops operator, archives meta)
        - stop_noop: stopped → stopped  (idempotent; logs and returns)
        - alive:     pending → running  (called from get_status on pending→running; also usable by reconciler)
    """

    allow_event_without_transition = False  # raise TransitionNotAllowed instead of silently ignoring invalid events
    catch_errors_as_events = False

    # States
    pending = SMState("Pending", initial=True, value=RockState.PENDING)
    running = SMState("Running", value=RockState.RUNNING)
    stopped = SMState("Stopped", value=RockState.STOPPED)

    # Transitions
    stop = pending.to(stopped) | running.to(stopped)
    stop_noop = stopped.to(stopped)
    alive = pending.to(running)

    def __init__(self, **kwargs):
        """Initialize with optional sandbox_info."""
        super().__init__(**kwargs)
        self.sandbox_info: SandboxInfo | None = kwargs.get("sandbox_info")

    # Callbacks

    async def on_stop(self, sandbox_id: str, operator, meta_store, reason: StopReason = StopReason.MANUAL) -> None:
        logger.info(f"stop sandbox {sandbox_id} (reason={reason.value})")
        sandbox_info = self.sandbox_info or {}

        # Initialize sandbox_info with default values if not set
        if "sandbox_id" not in sandbox_info:
            sandbox_info["sandbox_id"] = sandbox_id

        sandbox_info["state"] = RockState.STOPPED
        if sandbox_info.get("start_time"):
            sandbox_info["stop_time"] = get_iso8601_timestamp()
            log_billing_info(sandbox_info=sandbox_info)

        try:
            await operator.stop(sandbox_id, reason=reason)
        except ValueError as e:
            logger.error(f"ray get actor, actor {sandbox_id} not exist", exc_info=e)

        logger.info(f"sandbox {sandbox_id} stopped")
        await meta_store.archive(sandbox_id, sandbox_info)

        # Update self.sandbox_info for potential future use
        self.sandbox_info = sandbox_info

    async def on_stop_noop(self, sandbox_id: str) -> None:
        logger.info(f"Sandbox {sandbox_id} already stopped, skipping")

    async def on_alive(self, sandbox_id: str, meta_store, sandbox_info: SandboxInfo) -> None:
        sandbox_info["state"] = RockState.RUNNING
        if not sandbox_info.get("start_time"):
            sandbox_info["start_time"] = get_iso8601_timestamp()
        await meta_store.update(sandbox_id, sandbox_info)

        # Update self.sandbox_info for potential future use
        self.sandbox_info = sandbox_info

    @classmethod
    async def from_state_value(cls, state_value: str | None, sandbox_info: SandboxInfo) -> "SandboxStateMachine":
        """Create a state machine restored to *state_value* (from Redis/DB)."""
        state_map = {
            RockState.PENDING: "pending",
            RockState.RUNNING: "running",
            RockState.STOPPED: "stopped",
        }
        sm = (
            cls(start_value=state_map[state_value], sandbox_info=sandbox_info)
            if state_value and state_value in state_map
            else cls(sandbox_info=sandbox_info)
        )
        await sm.activate_initial_state()
        return sm
