"""
Sandbox State Machine using python-statemachine library.

Defines sandbox lifecycle states and transitions. The on_start / on_stop
callbacks contain the actual async business logic so the state machine is
the single place that owns both transition validation and execution.
"""

import datetime
import zoneinfo
from typing import Any

from statemachine import State as SMState
from statemachine import StateChart

from rock import env_vars
from rock.actions.sandbox.response import State as RockState
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.metrics.billing import log_billing_info
from rock.common.constants import DeleteReason, StopReason
from rock.config import ArchiveDirStorageConfig, ArchiveRegistryConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.logger import init_logger
from rock.sandbox.archive.constants import ArchiveKeys
from rock.sandbox.utils.timeout import SandboxTimeoutHelper
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils.system import get_iso8601_timestamp

logger = init_logger(__name__)


class SandboxLifecycleHelper:
    """Stateless helpers for sandbox lifecycle policies and timestamps."""

    @staticmethod
    def parse_iso8601_timestamp(value: str | None) -> datetime.datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return None
        return parsed

    @staticmethod
    def resolve_auto_archive_seconds(info: dict[str, Any]) -> int | None:
        return SandboxLifecycleHelper._resolve_user_seconds(info, "auto_archive_seconds")

    @staticmethod
    def resolve_auto_delete_seconds(info: dict[str, Any]) -> int | None:
        return SandboxLifecycleHelper._resolve_user_seconds(info, "auto_delete_seconds")

    @staticmethod
    def apply_effective_auto_transition_policy(info: dict[str, Any], config: DockerDeploymentConfig) -> None:
        archive_seconds = SandboxLifecycleHelper.resolve_auto_archive_seconds(info)
        delete_seconds = SandboxLifecycleHelper.resolve_auto_delete_seconds(info)
        if archive_seconds is not None:
            config.auto_archive_seconds = archive_seconds
            config.auto_delete_seconds = None
            config.remove_container = False
        elif delete_seconds is not None:
            config.auto_archive_seconds = None
            config.auto_delete_seconds = delete_seconds
            config.remove_container = delete_seconds == 0

    @staticmethod
    def _resolve_user_seconds(info: dict[str, Any], field: str) -> int | None:
        if field in info:
            raw = info[field]
        else:
            spec = info.get("spec") or {}
            raw = spec.get(field)
        return SandboxLifecycleHelper._coerce_seconds(raw)

    @staticmethod
    def _coerce_seconds(raw: Any) -> int | None:
        if raw is None:
            return None
        if not isinstance(raw, int | str):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def get_current_state_started_at(state_history: list[dict[str, str]], target_state: str) -> str:
        for record in reversed(state_history):
            if record.get("to_state") == target_state:
                return record.get("timestamp", "")
        return ""


class SandboxStateMachine(StateChart):
    """
    State machine for sandbox lifecycle management.

    States:
        - pending:   Sandbox is being created / starting
        - running:   Sandbox is actively running
        - stopped:   Sandbox has been stopped
        - deleted:   Sandbox has been soft-deleted (DB record kept, state=deleted)

    Transitions:
        - stop:      pending/running → stopped  (stops operator, archives meta)
        - stop_noop: stopped → stopped  (idempotent; logs and returns)
        - alive:     pending → running  (called from get_status on pending→running; also usable by reconciler)
        - delete:    stopped → deleted  (operator removes docker container, soft-deletes meta)
    """

    allow_event_without_transition = False  # raise TransitionNotAllowed instead of silently ignoring invalid events
    catch_errors_as_events = False

    # States
    pending = SMState("Pending", initial=True, value=RockState.PENDING)
    running = SMState("Running", value=RockState.RUNNING)
    stopped = SMState("Stopped", value=RockState.STOPPED)
    archiving = SMState("Archiving", value=RockState.ARCHIVING)
    archived = SMState("Archived", value=RockState.ARCHIVED)
    deleted = SMState("Deleted", final=True, value=RockState.DELETED)

    # Transitions
    stop = pending.to(stopped) | running.to(stopped)
    stop_noop = stopped.to(stopped)
    alive = pending.to(running)
    restart = stopped.to(pending)
    delete = stopped.to(deleted) | archived.to(deleted)
    archive = stopped.to(archiving)
    archive_done = archiving.to(archived)
    archive_failed = archiving.to(stopped)
    restore = archived.to(pending)
    restore_failed = pending.to(archived)

    def __init__(self, **kwargs):
        """Initialize with optional sandbox_info."""
        super().__init__(**kwargs)
        self.sandbox_info: SandboxInfo | None = kwargs.get("sandbox_info")

    # Callbacks

    def before_transition(self, event, source, target):
        if source.value == target.value:
            return
        if self.sandbox_info is None:
            self.sandbox_info = {}
        history = self.sandbox_info.setdefault("state_history", [])
        history.append(
            {
                "from_state": source.value.value,
                "to_state": target.value.value,
                "event": str(event),
                "timestamp": get_iso8601_timestamp(),
            }
        )
        # Cap history to avoid unbounded growth in long-lived sandboxes
        if len(history) > 100:
            del history[:-100]

    async def on_stop(
        self, sandbox_id: str, operator, meta_store, reason: StopReason = StopReason.MANUAL, auto_transition=None
    ) -> None:
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
        now = datetime.datetime.now(zoneinfo.ZoneInfo(env_vars.ROCK_TIME_ZONE))
        sandbox_info["stop_time"] = now.isoformat(timespec="seconds")
        sandbox_info["auto_transition_state"] = None
        sandbox_info["auto_transition_time"] = None
        if auto_transition:
            archive_seconds = SandboxLifecycleHelper.resolve_auto_archive_seconds(sandbox_info)
            transition_state = RockState.ARCHIVED
            transition_seconds = archive_seconds
            if archive_seconds is None:
                transition_state = RockState.DELETED
                transition_seconds = SandboxLifecycleHelper.resolve_auto_delete_seconds(sandbox_info)
            if transition_seconds is not None and transition_seconds >= 0:
                sandbox_info["auto_transition_state"] = transition_state
                sandbox_info["auto_transition_time"] = (now + datetime.timedelta(seconds=transition_seconds)).isoformat(
                    timespec="seconds"
                )
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
            phases = sandbox_info.get("phases", {})
            docker_run = phases.get("docker_run", {})
            sandbox_info["start_time"] = docker_run.get("completed_at") or get_iso8601_timestamp()
        if self.sandbox_info and "state_history" in self.sandbox_info:
            sandbox_info["state_history"] = self.sandbox_info["state_history"]
        if self.sandbox_info:
            for field in ("auto_archive_seconds", "auto_delete_seconds"):
                if field in self.sandbox_info:
                    sandbox_info[field] = self.sandbox_info[field]
        # Clear archive_time once the sandbox is fully live again — otherwise
        # _reconcile_pending would misclassify a subsequent plain restart as a
        # restore-in-progress and time it out to ARCHIVED.
        sandbox_info["archive_time"] = None
        sandbox_info["auto_transition_state"] = None
        sandbox_info["auto_transition_time"] = None
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
            SandboxLifecycleHelper.apply_effective_auto_transition_policy(info, restart_config)
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
        new_info["stop_time"] = None
        new_info["auto_transition_state"] = None
        new_info["auto_transition_time"] = None
        new_info.pop("phases", None)
        await meta_store.update(sandbox_id, new_info)
        await meta_store.update_timeout(sandbox_id, timeout_info)

    async def on_delete(
        self,
        sandbox_id: str,
        operator,
        meta_store,
        reason: DeleteReason = DeleteReason.MANUAL,
        dir_storage=None,
        image_storage=None,
    ) -> None:
        logger.info(f"delete sandbox {sandbox_id} (reason={reason.value})")
        sandbox_info = self.sandbox_info or {}
        if "sandbox_id" not in sandbox_info:
            sandbox_info["sandbox_id"] = sandbox_id

        host_ip = sandbox_info.get("host_ip")
        if reason == DeleteReason.IMMEDIATE:
            logger.info(f"sandbox {sandbox_id}: skip operator.delete (container already removed by --rm)")
        else:
            spec = sandbox_info.get("spec") or {}
            if spec:
                try:
                    delete_config = DockerDeploymentConfig(**spec)
                    await operator.delete(delete_config, host_ip=host_ip)
                except Exception as e:
                    logger.warning(f"operator.delete({sandbox_id}, {host_ip}) failed: {e}", exc_info=True)
            else:
                logger.warning(
                    f"sandbox {sandbox_id} has no spec snapshot; skip operator.delete, "
                    "rely on ContainerCleanupTask to reap docker container"
                )

        if sandbox_info.get("archive_time") and dir_storage and image_storage:
            prefix = sandbox_info.get("archive_prefix", ArchiveDirStorageConfig.prefix)
            registry_ns = sandbox_info.get("registry_namespace", ArchiveRegistryConfig.namespace)
            key = ArchiveKeys.dir_key(sandbox_id, prefix)
            ref = ArchiveKeys.image_ref(sandbox_id, image_storage.registry_url, registry_ns)
            logger.info(f"delete: cleaning up archive artifacts for {sandbox_id} (dir={key}, image={ref})")
            try:
                await dir_storage.delete(key)
            except Exception as e:
                logger.warning(f"delete: cleanup archive dir {key} failed: {e}")
            try:
                await image_storage.delete(ref)
            except Exception as e:
                logger.warning(f"delete: cleanup archive image {ref} failed: {e}")

        sandbox_info["state"] = RockState.DELETED
        sandbox_info["delete_time"] = get_iso8601_timestamp()
        sandbox_info["auto_transition_state"] = None
        sandbox_info["auto_transition_time"] = None
        await meta_store.archive(sandbox_id, sandbox_info)
        self.sandbox_info = sandbox_info

    async def on_archive(
        self,
        sandbox_id: str,
        meta_store,
        operator=None,
        dir_storage=None,
        image_storage=None,
        archive_params: dict | None = None,
    ) -> None:
        logger.info(f"archive sandbox {sandbox_id}")
        sandbox_info = self.sandbox_info or {}
        archive_params = archive_params or {}
        prefix = archive_params.get("archive_prefix", ArchiveDirStorageConfig.prefix)
        registry_ns = archive_params.get("registry_namespace", ArchiveRegistryConfig.namespace)

        sandbox_info["state"] = RockState.ARCHIVING
        sandbox_info["archive_prefix"] = prefix
        sandbox_info["registry_namespace"] = registry_ns
        sandbox_info["auto_transition_state"] = None
        sandbox_info["auto_transition_time"] = None
        await meta_store.archive(sandbox_id, sandbox_info)
        self.sandbox_info = sandbox_info

        if operator:
            spec = sandbox_info.get("spec") or {}
            config = DockerDeploymentConfig(**spec)
            await operator.start_archive(
                config=config,
                host_ip=sandbox_info.get("host_ip"),
                dir_storage_config=dir_storage.client_config,
                image_storage_config=image_storage.client_config,
                archive_params=archive_params,
            )

    async def on_archive_done(self, sandbox_id: str, meta_store, auto_transition=None) -> None:
        logger.info(f"archive done sandbox {sandbox_id}")
        sandbox_info = self.sandbox_info or {}
        now = datetime.datetime.now(zoneinfo.ZoneInfo(env_vars.ROCK_TIME_ZONE))
        sandbox_info["state"] = RockState.ARCHIVED
        sandbox_info["archive_time"] = now.isoformat(timespec="seconds")
        sandbox_info["auto_transition_state"] = None
        sandbox_info["auto_transition_time"] = None
        if auto_transition and auto_transition.auto_delete_archived_seconds is not None:
            sandbox_info["auto_transition_state"] = RockState.DELETED
            sandbox_info["auto_transition_time"] = (
                now + datetime.timedelta(seconds=auto_transition.auto_delete_archived_seconds)
            ).isoformat(timespec="seconds")
        await meta_store.archive(sandbox_id, sandbox_info)
        self.sandbox_info = sandbox_info

    async def on_archive_failed(self, sandbox_id: str, meta_store, reason: str = "", auto_transition=None) -> None:
        logger.info(f"archive failed sandbox {sandbox_id}: {reason}")
        sandbox_info = self.sandbox_info or {}
        sandbox_info["state"] = RockState.STOPPED
        sandbox_info["archive_time"] = None
        sandbox_info["auto_transition_state"] = None
        sandbox_info["auto_transition_time"] = None
        if auto_transition and auto_transition.auto_delete_seconds is not None:
            now = datetime.datetime.now(zoneinfo.ZoneInfo(env_vars.ROCK_TIME_ZONE))
            sandbox_info["auto_transition_state"] = RockState.DELETED
            sandbox_info["auto_transition_time"] = (
                now + datetime.timedelta(seconds=auto_transition.auto_delete_seconds)
            ).isoformat(timespec="seconds")
        await meta_store.archive(sandbox_id, sandbox_info)
        self.sandbox_info = sandbox_info

    async def on_restore(
        self,
        sandbox_id: str,
        meta_store,
        operator=None,
        dir_storage=None,
        image_storage=None,
        restore_timeout_seconds: int | None = None,
    ) -> None:
        """ARCHIVED → PENDING: fire-and-forget actor does pull+download+docker start.

        Writes to Redis so that operator.get_status can probe alive status
        (PENDING alive detection drives the PENDING → RUNNING transition).
        """
        logger.info(f"restore sandbox {sandbox_id}")
        sandbox_info = self.sandbox_info or {}
        sandbox_info["state"] = RockState.PENDING
        sandbox_info["stop_time"] = None
        sandbox_info["auto_transition_state"] = None
        sandbox_info["auto_transition_time"] = None
        await meta_store.update(sandbox_id, sandbox_info)

        spec = sandbox_info.get("spec") or {}
        if spec:
            config = DockerDeploymentConfig(**spec)
            SandboxLifecycleHelper.apply_effective_auto_transition_policy(sandbox_info, config)
            timeout_info = SandboxTimeoutHelper.make_timeout_info(config.auto_clear_time)
            if timeout_info:
                await meta_store.update_timeout(sandbox_id, timeout_info)
        self.sandbox_info = sandbox_info

        if operator:
            archive_params = {
                "archive_prefix": sandbox_info.get("archive_prefix", ArchiveDirStorageConfig.prefix),
                "registry_namespace": sandbox_info.get("registry_namespace", ArchiveRegistryConfig.namespace),
            }
            if restore_timeout_seconds:
                archive_params["timeout_seconds"] = restore_timeout_seconds
            config = DockerDeploymentConfig(**spec)
            SandboxLifecycleHelper.apply_effective_auto_transition_policy(sandbox_info, config)
            new_host_ip = await operator.start_restore(
                config=config,
                dir_storage_config=dir_storage.client_config,
                image_storage_config=image_storage.client_config,
                archive_params=archive_params,
            )
            if new_host_ip and new_host_ip != sandbox_info.get("host_ip"):
                logger.info(f"restore sandbox {sandbox_id} scheduled on new host {new_host_ip}")
                sandbox_info["host_ip"] = new_host_ip
                await meta_store.update(sandbox_id, sandbox_info)
                self.sandbox_info = sandbox_info

    async def on_restore_failed(self, sandbox_id: str, meta_store, reason: str = "", auto_transition=None) -> None:
        """PENDING → ARCHIVED: timeout or unrecoverable error during restore."""
        logger.info(f"restore failed sandbox {sandbox_id}: {reason}")
        sandbox_info = self.sandbox_info or {}
        sandbox_info["state"] = RockState.ARCHIVED
        sandbox_info["auto_transition_state"] = None
        sandbox_info["auto_transition_time"] = None
        if auto_transition and auto_transition.auto_delete_archived_seconds is not None:
            now = datetime.datetime.now(zoneinfo.ZoneInfo(env_vars.ROCK_TIME_ZONE))
            sandbox_info["auto_transition_state"] = RockState.DELETED
            sandbox_info["auto_transition_time"] = (
                now + datetime.timedelta(seconds=auto_transition.auto_delete_archived_seconds)
            ).isoformat(timespec="seconds")
        await meta_store.archive(sandbox_id, sandbox_info)
        self.sandbox_info = sandbox_info

    @classmethod
    async def from_state_value(cls, state_value: str | None, sandbox_info: SandboxInfo) -> "SandboxStateMachine":
        """Create a state machine restored to *state_value* (from Redis/DB)."""
        state_map = {
            RockState.PENDING: "pending",
            RockState.RUNNING: "running",
            RockState.STOPPED: "stopped",
            RockState.ARCHIVING: "archiving",
            RockState.ARCHIVED: "archived",
            RockState.DELETED: "deleted",
        }
        sm = (
            cls(start_value=state_map[state_value], sandbox_info=sandbox_info)
            if state_value and state_value in state_map
            else cls(sandbox_info=sandbox_info)
        )
        await sm.activate_initial_state()
        return sm
