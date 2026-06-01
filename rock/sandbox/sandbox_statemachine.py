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
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.utils.timeout import SandboxTimeoutHelper
from rock.sdk.common.exceptions import BadRequestRockError
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
    restart = stopped.to(pending)

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
        # Always record stop_time — sandboxes that never started (e.g. image
        # pull / docker run failed before sandbox_actor wrote start_time) also
        # need this for downstream consumers like SandboxLogArchiveTask. The
        # billing call below stays gated on start_time because billing is
        # only meaningful for actually-started sandboxes.
        sandbox_info["stop_time"] = get_iso8601_timestamp()
        if sandbox_info.get("start_time"):
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

    async def on_restart(self, sandbox_id: str, operator, meta_store) -> None:
        info = self.sandbox_info or {}

        host_ip = info.get("host_ip")
        if not host_ip:
            raise BadRequestRockError(f"Sandbox {sandbox_id} has no host_ip; cannot pin restart to original node")

        # Prefer the spec snapshot (DockerDeploymentConfig.model_dump persisted to
        # the DB at start time) so the new actor wraps the existing container with
        # the exact same config.
        spec = info.get("spec") or {}
        if spec:
            restart_config = DockerDeploymentConfig(**spec)
        else:
            logger.warning(
                f"sandbox {sandbox_id} has no spec snapshot; rebuilding config from flat fields with model defaults"
            )
            restart_config = DockerDeploymentConfig(
                container_name=sandbox_id,
                image=info.get("image") or DockerDeploymentConfig.model_fields["image"].default,
                memory=info.get("memory") or DockerDeploymentConfig.model_fields["memory"].default,
                cpus=float(info.get("cpus") or DockerDeploymentConfig.model_fields["cpus"].default),
            )
        timeout_info = SandboxTimeoutHelper.make_timeout_info(restart_config.auto_clear_time)

        logger.info(f"restart sandbox {sandbox_id} (pin host_ip={host_ip})")
        await operator.restart(restart_config, host_ip=host_ip)

        # The previous stop() called meta_store.archive() which removed the Redis
        # alive key, so a partial update would lose every field except `state`.
        # Re-seed Redis with the full sandbox_info restored from the DB.
        # meta_store.update filters to SandboxInfo-declared keys, so DB-only
        # fields (spec/status) won't pollute the alive key.
        new_info = dict(info)
        new_info["state"] = RockState.PENDING
        new_info.pop("stop_time", None)
        await meta_store.update(sandbox_id, new_info)
        await meta_store.update_timeout(sandbox_id, timeout_info)

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
