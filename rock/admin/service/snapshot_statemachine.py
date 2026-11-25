"""Durable lifecycle state machine for the ROCK snapshot catalog."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping
from typing import Any

from statemachine import State as SMState
from statemachine import StateChart
from statemachine.exceptions import TransitionNotAllowed

from rock.admin.core.snapshot_table import SnapshotIdentityConflict, SnapshotTable

MAX_SNAPSHOT_SPEC_BYTES = 1024 * 1024


class InvalidSnapshotTransition(ValueError):
    """The requested event is not valid for the durable snapshot state."""


class SnapshotStateMachine(StateChart):
    """Validate one snapshot event and commit it through PostgreSQL CAS.

    Instances are deliberately short lived.  Callers restore one from the
    latest catalog row, send at most one event, and then discard it.  The
    in-memory StateChart state is never a durable authority.
    """

    allow_event_without_transition = False
    catch_errors_as_events = False

    creating = SMState("Creating", initial=True, value="CREATING")
    local_ready_state = SMState("Local Ready", value="LOCAL_READY")
    remote_ready_state = SMState("Remote Ready", value="REMOTE_READY")
    failed = SMState("Failed", value="FAILED")
    deleted = SMState("Deleted", final=True, value="DELETED")

    local_ready = creating.to(local_ready_state)
    remote_ready = local_ready_state.to(remote_ready_state)
    fail = creating.to(failed) | local_ready_state.to(failed)
    delete = remote_ready_state.to(deleted) | failed.to(deleted)

    def __init__(
        self,
        *,
        snapshot: dict[str, Any],
        snapshot_table: SnapshotTable,
        clock: Callable[[], datetime.datetime] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._snapshot = dict(snapshot)
        self._snapshot_table = snapshot_table
        self._clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))

    @property
    def snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot)

    @classmethod
    async def from_record(
        cls,
        snapshot: dict[str, Any],
        snapshot_table: SnapshotTable,
        *,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> SnapshotStateMachine:
        state_value = snapshot.get("state")
        state_values = {state.value for state in cls.states}
        if state_value not in state_values:
            raise InvalidSnapshotTransition(f"unknown durable snapshot state {state_value!r}")
        machine = cls(
            start_value=state_value,
            snapshot=snapshot,
            snapshot_table=snapshot_table,
            clock=clock,
        )
        await machine.activate_initial_state()
        return machine

    async def send(self, event: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await super().send(event, *args, **kwargs)
        except TransitionNotAllowed as error:
            raise InvalidSnapshotTransition(
                f"snapshot event {event!r} is not allowed from state {self._snapshot['state']!r}"
            ) from error

    async def on_local_ready(self) -> dict[str, Any]:
        return await self._commit("LOCAL_READY")

    async def on_remote_ready(
        self,
        *,
        spec: dict[str, Any] | None = None,
        memory_bytes: int | None = None,
        rootfs_bytes: int | None = None,
    ) -> dict[str, Any]:
        if spec is None:
            raise InvalidSnapshotTransition("remote_ready requires a snapshot spec")
        if not isinstance(spec, dict):
            raise InvalidSnapshotTransition("snapshot spec must be a JSON object")
        try:
            encoded = json.dumps(spec, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError) as error:
            raise InvalidSnapshotTransition("snapshot spec must be JSON serializable") from error
        if len(encoded) > MAX_SNAPSHOT_SPEC_BYTES:
            raise InvalidSnapshotTransition("snapshot spec exceeds maximum encoded size")
        # The descriptor belongs to snapd. Do not interpret or strip its fields.
        stored_spec = json.loads(encoded)
        values: dict[str, Any] = {"spec": stored_spec, "remote_ready_at": self._clock()}
        identity: dict[str, Any] = {"spec": stored_spec}
        sizes = {
            key: value
            for key, value in {"memory_bytes": memory_bytes, "rootfs_bytes": rootfs_bytes}.items()
            if value is not None
        }
        if any(type(value) is not int or value <= 0 for value in sizes.values()):
            raise InvalidSnapshotTransition("snapshot sizes must be positive integers")
        if sizes:
            resources = {**(self._snapshot.get("resource_spec") or {}), **sizes}
            values["resource_spec"] = resources
            identity["resource_spec"] = resources
        return await self._commit(
            "REMOTE_READY",
            values=values,
            replay_identity_values=identity,
        )

    async def on_fail(self, *, error_context: str | None = None) -> dict[str, Any]:
        del error_context
        return await self._commit("FAILED", replay_identity_values={"spec": None})

    async def on_delete(self) -> dict[str, Any]:
        return await self._commit("DELETED", values={"deleted_at": self._clock()})

    async def _commit(
        self,
        target_state: str,
        *,
        values: dict[str, Any] | None = None,
        replay_identity_values: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot_id = self._snapshot["snapshot_id"]
        expected_state = self._snapshot["state"]
        replay_states = self._safe_replay_states(target_state)
        committed = await self._snapshot_table.conditional_transition(
            snapshot_id,
            expected_state=expected_state,
            new_state=target_state,
            values=values,
            acceptable_replay_states=replay_states,
            replay_identity_values=replay_identity_values,
        )
        if committed is None:
            committed = await self._resolve_cas_loser(
                target_state,
                replay_states=replay_states,
                replay_identity_values=replay_identity_values,
            )
        self._snapshot = dict(committed)
        return dict(committed)

    async def _resolve_cas_loser(
        self,
        target_state: str,
        *,
        replay_states: frozenset[str],
        replay_identity_values: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        observed = await self._snapshot_table.get(self._snapshot["snapshot_id"])
        if observed is None:
            raise InvalidSnapshotTransition("snapshot disappeared during its durable transition")
        if observed["state"] != target_state and observed["state"] not in replay_states:
            raise InvalidSnapshotTransition(f"snapshot transition lost a concurrent CAS to state {observed['state']!r}")
        identity_values = dict(replay_identity_values or {})
        if not all(observed[field] == value for field, value in identity_values.items()):
            raise SnapshotIdentityConflict("snapshot transition committed with different values")
        return observed

    @staticmethod
    def _safe_replay_states(target_state: str) -> frozenset[str]:
        """Return descendants that prove this exact event already committed.

        FAILED and DELETED are join states.  A bare DELETED row therefore does
        not prove that LOCAL_READY committed; only REMOTE_READY does.  Spec or
        ``spec is None`` is used as the identity proof for the other joins.
        """

        safe = {
            "LOCAL_READY": frozenset({"REMOTE_READY"}),
            "REMOTE_READY": frozenset({"DELETED"}),
            "FAILED": frozenset({"DELETED"}),
            "DELETED": frozenset(),
        }
        if target_state not in safe:
            raise InvalidSnapshotTransition(f"unknown snapshot state {target_state!r}")
        return safe[target_state]
