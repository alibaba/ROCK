"""Database access for the canonical ``rock_snapshot`` catalog."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import re
from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import DisconnectionError, IntegrityError, InterfaceError, OperationalError

from rock.admin.core.db_provider import DatabaseProvider, retry_on_disconnect
from rock.admin.core.schema import RockSnapshotRecord

_TRANSITION_VALUE_COLUMNS = frozenset({"spec", "resource_spec", "remote_ready_at", "deleted_at"})
_MUTATION_ATTEMPTS = 4
_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OperationalError,
    InterfaceError,
    DisconnectionError,
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
)
_MUTATION_EXCEPTIONS = _RETRY_EXCEPTIONS + (IntegrityError,)


class SnapshotIdentityConflict(ValueError):
    pass


def validate_snapshot_id(snapshot_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", snapshot_id) is None:
        raise ValueError("snapshot_id is not a canonical path segment")
    return snapshot_id


def _validated_worker_ip(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as error:
        raise ValueError("source_worker_ip must be an IPv4 or IPv6 literal") from error


@dataclass(frozen=True, slots=True)
class BaseImageResolutionIdentity:
    oci_ref: str
    oci_digest: str
    oci_platform: str
    base_image_builder_version: str
    base_image_build_key: str
    source_worker_ip: str


@dataclass(frozen=True, slots=True)
class BaseImageClaim:
    snapshot: dict[str, Any]
    dispatch: bool
    reused: bool


def _as_dict(record: RockSnapshotRecord) -> dict[str, Any]:
    result = {column.key: getattr(record, column.key) for column in record.__table__.columns}
    # SQLite drops timezone metadata even for DateTime(timezone=True). The
    # catalog writes UTC only, so normalise SQLite and PostgreSQL reads to the
    # same aware UTC representation at the repository boundary.
    for field in ("created_at", "remote_ready_at", "deleted_at"):
        value = result[field]
        if value is None:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        else:
            value = value.astimezone(datetime.timezone.utc)
        result[field] = value
    return result


class SnapshotTable:
    """Portable repository with atomic expected-state transitions."""

    def __init__(self, db_provider: DatabaseProvider) -> None:
        self._db = db_provider

    async def claim_base_image(
        self,
        *,
        snapshot_id: str,
        resolution: BaseImageResolutionIdentity,
        created_at: datetime.datetime,
        format_version: int = 1,
    ) -> BaseImageClaim:
        """Resolve an allocated build identity, or join a canonical OCI import."""

        validate_snapshot_id(snapshot_id)
        worker_ip = _validated_worker_ip(resolution.source_worker_ip)
        assert worker_ip is not None
        normalized = BaseImageResolutionIdentity(
            oci_ref=resolution.oci_ref,
            oci_digest=resolution.oci_digest,
            oci_platform=resolution.oci_platform,
            base_image_builder_version=resolution.base_image_builder_version,
            base_image_build_key=resolution.base_image_build_key,
            source_worker_ip=worker_ip,
        )
        last_error: BaseException | None = None
        for attempt in range(_MUTATION_ATTEMPTS):
            try:
                return await self._db.run(
                    self._claim_base_image_sync,
                    snapshot_id,
                    normalized,
                    created_at,
                    format_version,
                )
            except _MUTATION_EXCEPTIONS as error:
                last_error = error
                if isinstance(error, IntegrityError) and attempt + 1 == _MUTATION_ATTEMPTS:
                    raise SnapshotIdentityConflict("BASE_IMAGE build-key claim did not converge") from error
                if attempt + 1 < _MUTATION_ATTEMPTS:
                    await asyncio.sleep(0 if isinstance(error, IntegrityError) else 2**attempt)
        assert last_error is not None
        raise last_error

    def _claim_base_image_sync(
        self,
        snapshot_id: str,
        resolution: BaseImageResolutionIdentity,
        created_at: datetime.datetime,
        format_version: int,
    ) -> BaseImageClaim:
        identity_fields = {
            "oci_digest": resolution.oci_digest,
            "oci_platform": resolution.oci_platform,
            "base_image_builder_version": resolution.base_image_builder_version,
            "base_image_build_key": resolution.base_image_build_key,
            "format_version": format_version,
        }
        with self._db.session_factory() as session, session.begin():
            requested_snapshot = session.get(RockSnapshotRecord, snapshot_id)
            if requested_snapshot is not None:
                if requested_snapshot.kind != "BASE_IMAGE" or requested_snapshot.state == "DELETED":
                    raise SnapshotIdentityConflict("snapshot ID is already bound to another artifact")
                current_identity = {
                    field: getattr(requested_snapshot, field) for field in identity_fields
                }
                if requested_snapshot.oci_digest is None and requested_snapshot.state == "CREATING":
                    result = session.execute(
                        update(RockSnapshotRecord)
                        .where(
                            RockSnapshotRecord.snapshot_id == snapshot_id,
                            RockSnapshotRecord.oci_digest.is_(None),
                            RockSnapshotRecord.state == "CREATING",
                        )
                        .values(
                            **identity_fields, oci_ref=resolution.oci_ref,
                            source_worker_ip=resolution.source_worker_ip,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if result.rowcount != 1:
                        raise SnapshotIdentityConflict("BASE_IMAGE build identity changed concurrently")
                    session.refresh(requested_snapshot)
                elif current_identity != identity_fields:
                    raise SnapshotIdentityConflict(
                        "snapshot ID was committed with another immutable OCI identity"
                    )

            snapshot = requested_snapshot or session.execute(
                select(RockSnapshotRecord)
                .where(
                    RockSnapshotRecord.kind == "BASE_IMAGE",
                    RockSnapshotRecord.base_image_build_key == resolution.base_image_build_key,
                    RockSnapshotRecord.state != "DELETED",
                    RockSnapshotRecord.product_metadata.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            reused = snapshot is not None
            if snapshot is None:
                snapshot = RockSnapshotRecord(
                    snapshot_id=snapshot_id,
                    kind="BASE_IMAGE",
                    source_worker_ip=resolution.source_worker_ip,
                    oci_ref=resolution.oci_ref,
                    state="CREATING",
                    created_at=created_at,
                    **identity_fields,
                )
                session.add(snapshot)
            else:
                if any(getattr(snapshot, field) != value for field, value in identity_fields.items()):
                    raise SnapshotIdentityConflict("BASE_IMAGE build key has another immutable OCI identity")
                if snapshot.state == "FAILED":
                    snapshot.state = "CREATING"
            # The partial unique index chooses the winner across Admin replicas.
            # Joiners retain its original request and owner so snapd can safely
            # deduplicate redispatch, including after a crash before the RPC.
            session.flush()
            return BaseImageClaim(
                snapshot=_as_dict(snapshot),
                dispatch=snapshot.state == "CREATING",
                reused=reused,
            )

    async def create(
        self,
        *,
        snapshot_id: str,
        kind: str,
        created_at: datetime.datetime,
        source_sandbox_id: str | None = None,
        source_worker_ip: str | None = None,
        format_version: int = 1,
        resource_spec: dict[str, Any] | None = None,
        product_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_worker_ip = _validated_worker_ip(source_worker_ip)
        if kind == "FULL":
            try:
                return await self._db.run(
                    self._create_sync,
                    snapshot_id,
                    kind,
                    created_at,
                    source_sandbox_id,
                    source_worker_ip,
                    format_version,
                    deepcopy(resource_spec),
                    deepcopy(product_metadata),
                )
            except IntegrityError as error:
                if await self.get(snapshot_id) is not None:
                    raise SnapshotIdentityConflict("snapshot ID already exists") from error
                raise
        expected = {
            "snapshot_id": snapshot_id,
            "kind": kind,
            "source_sandbox_id": source_sandbox_id,
            "source_worker_ip": source_worker_ip,
            "format_version": format_version,
            "resource_spec": resource_spec,
            "product_metadata": product_metadata,
        }
        last_error: BaseException | None = None
        for attempt in range(_MUTATION_ATTEMPTS):
            try:
                return await self._db.run(
                    self._create_sync,
                    snapshot_id,
                    kind,
                    created_at,
                    source_sandbox_id,
                    source_worker_ip,
                    format_version,
                    deepcopy(resource_spec),
                    deepcopy(product_metadata),
                )
            except _MUTATION_EXCEPTIONS as error:
                last_error = error
                existing = await self.get(snapshot_id)
                if existing is not None:
                    if all(existing[field] == value for field, value in expected.items()):
                        return existing
                    raise SnapshotIdentityConflict(
                        "snapshot ID was committed with another immutable identity"
                    ) from error
                if isinstance(error, IntegrityError):
                    raise
                if attempt + 1 < _MUTATION_ATTEMPTS:
                    await asyncio.sleep(2**attempt)
        assert last_error is not None
        raise last_error

    def _create_sync(
        self,
        snapshot_id: str,
        kind: str,
        created_at: datetime.datetime,
        source_sandbox_id: str | None,
        source_worker_ip: str | None,
        format_version: int,
        resource_spec: dict[str, Any] | None,
        product_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = RockSnapshotRecord(
            snapshot_id=snapshot_id,
            kind=kind,
            source_sandbox_id=source_sandbox_id,
            source_worker_ip=source_worker_ip,
            format_version=format_version,
            resource_spec=resource_spec,
            product_metadata=product_metadata,
            state="CREATING",
            created_at=created_at,
        )
        with self._db.session_factory() as session:
            session.add(record)
            session.commit()
            return _as_dict(record)

    async def create_full_snapshot_attempt(
        self,
        *,
        snapshot_id: str,
        source_sandbox_id: str,
        source_worker_ip: str,
        resource_spec: dict[str, Any],
        created_at: datetime.datetime,
        format_version: int = 1,
    ) -> dict[str, Any]:
        """Insert a new attempt; snapd arbitrates concurrent source cuts.

        Snapshot IDs cannot be reused. This never locks or writes the source row.
        """

        validate_snapshot_id(snapshot_id)
        if not source_sandbox_id or len(source_sandbox_id.encode()) > 128:
            raise ValueError("source_sandbox_id must contain 1..128 UTF-8 bytes")
        source_worker_ip = _validated_worker_ip(source_worker_ip)
        if source_worker_ip is None:
            raise ValueError("source_worker_ip is required")
        return await self.create(
            snapshot_id=snapshot_id,
            kind="FULL",
            created_at=created_at,
            source_sandbox_id=source_sandbox_id,
            source_worker_ip=source_worker_ip,
            format_version=format_version,
            resource_spec=resource_spec,
        )

    @retry_on_disconnect
    async def get(self, snapshot_id: str) -> dict[str, Any] | None:
        return await self._db.run(self._get_sync, snapshot_id)

    def _get_sync(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._db.session_factory() as session:
            record = session.get(RockSnapshotRecord, snapshot_id)
            return None if record is None else _as_dict(record)

    @retry_on_disconnect
    async def list_by_state(
        self,
        state: str,
        *,
        limit: int = 100,
        after: tuple[datetime.datetime, str] | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        return await self._db.run(self._list_by_state_sync, state, limit, after)

    def _list_by_state_sync(
        self, state: str, limit: int, after: tuple[datetime.datetime, str] | None,
    ) -> list[dict[str, Any]]:
        statement = (
            select(RockSnapshotRecord)
            .where(RockSnapshotRecord.state == state)
            .order_by(RockSnapshotRecord.created_at.asc(), RockSnapshotRecord.snapshot_id.asc())
            .limit(limit)
        )
        if after is not None:
            created_at, snapshot_id = after
            statement = statement.where(or_(
                RockSnapshotRecord.created_at > created_at,
                and_(RockSnapshotRecord.created_at == created_at, RockSnapshotRecord.snapshot_id > snapshot_id),
            ))
        with self._db.session_factory() as session:
            records = session.execute(statement).scalars().all()
            return [_as_dict(record) for record in records]

    @retry_on_disconnect
    async def list(
        self,
        *,
        source_sandbox_id: str | None = None,
        include_deleted: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if offset < 0 or limit <= 0 or limit > 1000:
            raise ValueError("offset must be non-negative and limit must be in 1..1000")
        return await self._db.run(self._list_sync, source_sandbox_id, include_deleted, offset, limit)

    def _list_sync(
        self,
        source_sandbox_id: str | None,
        include_deleted: bool,
        offset: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        statement = select(RockSnapshotRecord)
        if source_sandbox_id is not None:
            statement = statement.where(RockSnapshotRecord.source_sandbox_id == source_sandbox_id)
        if not include_deleted:
            statement = statement.where(RockSnapshotRecord.state != "DELETED")
        statement = statement.order_by(RockSnapshotRecord.created_at.desc()).offset(offset).limit(limit)
        with self._db.session_factory() as session:
            return [_as_dict(record) for record in session.execute(statement).scalars().all()]

    async def conditional_transition(
        self,
        snapshot_id: str,
        *,
        expected_state: str,
        new_state: str,
        values: dict[str, Any] | None = None,
        acceptable_replay_states: Collection[str] = (),
        replay_identity_values: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        transition_values = dict(values or {})
        unsupported = transition_values.keys() - _TRANSITION_VALUE_COLUMNS
        if unsupported:
            raise ValueError(f"unsupported snapshot transition values: {sorted(unsupported)}")
        if "resource_spec" in transition_values and (expected_state, new_state) != ("LOCAL_READY", "REMOTE_READY"):
            raise ValueError("unsupported snapshot transition values: ['resource_spec']")
        transition_values["state"] = new_state
        last_error: BaseException | None = None
        for attempt in range(_MUTATION_ATTEMPTS):
            try:
                return await self._db.run(
                    self._conditional_transition_sync,
                    snapshot_id,
                    expected_state,
                    transition_values,
                )
            except _RETRY_EXCEPTIONS as error:
                last_error = error
                existing = await self.get(snapshot_id)
                if existing is not None and (
                    existing["state"] == new_state or existing["state"] in acceptable_replay_states
                ):
                    identity_values = dict(replay_identity_values or {})
                    if all(existing[field] == value for field, value in identity_values.items()):
                        return existing
                    raise SnapshotIdentityConflict("snapshot transition committed with different values") from error
                if existing is not None and existing["state"] != expected_state:
                    return None
                if attempt + 1 < _MUTATION_ATTEMPTS:
                    await asyncio.sleep(2**attempt)
        assert last_error is not None
        raise last_error

    def _conditional_transition_sync(
        self,
        snapshot_id: str,
        expected_state: str,
        transition_values: dict[str, Any],
    ) -> dict[str, Any] | None:
        statement = (
            update(RockSnapshotRecord)
            .where(RockSnapshotRecord.snapshot_id == snapshot_id)
            .where(RockSnapshotRecord.state == expected_state)
            .values(**transition_values)
            .returning(RockSnapshotRecord)
        )
        with self._db.session_factory() as session:
            record = session.execute(statement).scalar_one_or_none()
            if record is None:
                session.rollback()
                return None
            session.commit()
            return _as_dict(record)
