"""ORM base and models for the ROCK admin database.

``Base`` is the single SQLAlchemy ``DeclarativeBase`` for all ROCK tables.
``SandboxRecord`` is the canonical persistence model for sandbox metadata.
"""

from __future__ import annotations

import datetime
import zoneinfo
from typing import Any, ClassVar

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import JSON

from rock import env_vars

_JSONB_VARIANT = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


# All ORM models that inherit from Base must be imported (or defined) in this file
# so that Base.metadata is fully populated before DatabaseProvider.init() calls
# create_all.  When adding a new table in a separate module, add its import here:
#   from rock.admin.core.<module> import <Model>  # noqa: F401
class SandboxRecord(Base):
    """ORM model for the ``sandbox_record`` table."""

    __tablename__ = "sandbox_record"

    sandbox_id = Column(String(128), primary_key=True)
    user_id = Column(String(128), nullable=False, default="default")
    image = Column(String(512), nullable=False, default="default")
    experiment_id = Column(String(128), nullable=False, default="default")
    namespace = Column(String(128), nullable=False, default="default")
    cluster_name = Column(String(128), nullable=False, default="default")
    state = Column(String(32), nullable=False, default="pending")
    host_ip = Column(String(128), nullable=False, default="default")
    create_time = Column(String(64), nullable=False, default="")
    start_time = Column(String(64), nullable=True)
    stop_time = Column(String(64), nullable=True)
    archive_time = Column(String(64), nullable=True)
    delete_time = Column(String(64), nullable=True)
    auto_transition_state = Column(String(32), nullable=True)
    auto_transition_time = Column(DateTime(timezone=True), nullable=True)
    host_name = Column(String(255), nullable=True)
    auth_token = Column(String(512), nullable=True)
    rock_authorization_encrypted = Column(String(1024), nullable=True)
    cpus = Column(Float, nullable=True)
    memory = Column(String(64), nullable=True)
    disk = Column(String(64), nullable=True)
    create_user_gray_flag = Column(Boolean, nullable=True)
    phases = Column(_JSONB_VARIANT, nullable=True)
    port_mapping = Column(_JSONB_VARIANT, nullable=True)
    labels = Column(
        _JSONB_VARIANT,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    spec = Column(_JSONB_VARIANT, nullable=True)
    status = Column(_JSONB_VARIANT, nullable=True)
    source_snapshot_id = Column(
        String(128),
        ForeignKey("rock_snapshot.snapshot_id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_sandbox_record_user_id", "user_id"),
        Index("ix_sandbox_record_state", "state"),
        Index("ix_sandbox_record_image", "image"),
        Index(
            "ix_sandbox_record_labels_gin",
            "labels",
            postgresql_using="gin",
            postgresql_ops={"labels": "jsonb_path_ops"},
        ),
        Index("ix_sandbox_record_state_stop_time", "state", "stop_time"),
        Index(
            "ix_sandbox_record_state_auto_transition",
            "state",
            "auto_transition_state",
            "auto_transition_time",
        ),
        Index("ix_sandbox_record_state_start_time", "state", "start_time"),
        Index(
            "sandbox_record_source_snapshot_ix",
            "source_snapshot_id",
            postgresql_where=text("source_snapshot_id IS NOT NULL"),
            sqlite_where=text("source_snapshot_id IS NOT NULL"),
        ),
    )

    # Columns allowed as the filter key in list_by().
    # Only include columns with an index (or PK); exclude JSONB, sensitive, and internal columns.
    LIST_BY_ALLOWLIST: ClassVar[frozenset[str]] = frozenset(
        {
            "sandbox_id",
            "user_id",
            "state",
            "image",
            "source_snapshot_id",
        }
    )
    _column_names: ClassVar[set[str] | None] = None

    @classmethod
    def column_names(cls) -> set[str]:
        if cls._column_names is None:
            cls._column_names = {c.key for c in cls.__table__.columns}
        return cls._column_names

    _NOT_NULL_DEFAULTS: ClassVar[dict[str, Any]] = {
        "user_id": "default",
        "image": "default",
        "experiment_id": "default",
        "namespace": "default",
        "cluster_name": "default",
        "state": "pending",
        "host_ip": "default",
        "create_time": "",
        "labels": {},
    }

    def to_dict(self) -> dict[str, Any]:
        """Return all non-``None`` column values as a plain dict."""
        result: dict[str, Any] = {}
        for column in self.__table__.columns:
            value = getattr(self, column.key)
            if value is None:
                continue
            if column.key == "auto_transition_time" and isinstance(value, datetime.datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=zoneinfo.ZoneInfo(env_vars.ROCK_TIME_ZONE))
                else:
                    value = value.astimezone(zoneinfo.ZoneInfo(env_vars.ROCK_TIME_ZONE))
                value = value.isoformat(timespec="seconds")
            result[column.key] = value
        return result


class TemplateRecord(Base):
    """ORM model for the ``template`` table."""

    __tablename__ = "template"

    template_id = Column(String(128), primary_key=True)
    image = Column(String(512), nullable=True)
    os_type = Column(String(32), nullable=False)
    cpu_count = Column(Integer, nullable=False)
    memory_mb = Column(Integer, nullable=False)
    disk_size_mb = Column(Integer, nullable=False)
    spec = Column(_JSONB_VARIANT, nullable=True)
    status = Column(String(32), nullable=False)
    current_step = Column(String(32), nullable=True)
    artifact_uri = Column(String(1024), nullable=True)
    execution_context = Column(_JSONB_VARIANT, nullable=True)
    error_code = Column(String(128), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_template_status", "status"),
        Index("ux_template_image", "image", unique=True),
    )


class SchedulerTaskRecord(Base):
    """One row per scheduled task execution. Grouped by taskset_id."""

    __tablename__ = "scheduler_task"

    task_id = Column(String(32), primary_key=True)
    taskset_id = Column(String(32), nullable=True, index=True)
    task_type = Column(String(64), nullable=False, index=True)
    target_workers = Column(_JSONB_VARIANT, nullable=True)
    creation_timestamp = Column(Float, nullable=False)
    phase = Column(String(32), nullable=False, default="Pending")
    assigned_pod = Column(String(128), nullable=False, default="")
    start_time = Column(Float, nullable=True)
    completion_time = Column(Float, nullable=True)
    conditions = Column(_JSONB_VARIANT, nullable=True)
    status = Column(_JSONB_VARIANT, nullable=True)


class RockSnapshotRecord(Base):
    """Canonical snapshot and template artifact catalog."""

    __tablename__ = "rock_snapshot"

    snapshot_id = Column(String(128), primary_key=True)
    kind = Column(String(16), nullable=False)
    source_sandbox_id = Column(String(128), nullable=True)
    source_worker_ip = Column(String(128), nullable=True)
    format_version = Column(Integer, nullable=False, default=1, server_default=text("1"))
    oci_ref = Column(String(1024), nullable=True)
    oci_digest = Column(String(71), nullable=True)
    oci_platform = Column(String(256), nullable=True)
    base_image_builder_version = Column(String(256), nullable=True)
    base_image_build_key = Column(String(71), nullable=True)
    product_metadata = Column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"), nullable=True,
    )
    resource_spec = Column(_JSONB_VARIANT, nullable=True)
    spec = Column(_JSONB_VARIANT, nullable=True)
    state = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    remote_ready_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('BASE_IMAGE', 'FULL')", name="rock_snapshot_kind_ck"),
        CheckConstraint("format_version >= 1", name="rock_snapshot_format_version_ck"),
        CheckConstraint(
            "state IN ('CREATING', 'LOCAL_READY', 'REMOTE_READY', 'DELETED', 'FAILED')",
            name="rock_snapshot_state_ck",
        ),
        CheckConstraint(
            "(kind = 'BASE_IMAGE' AND source_sandbox_id IS NULL) "
            "OR (kind = 'FULL' AND source_sandbox_id IS NOT NULL)",
            name="rock_snapshot_source_ck",
        ),
        CheckConstraint(
            "state <> 'REMOTE_READY' OR (spec IS NOT NULL AND remote_ready_at IS NOT NULL)",
            name="rock_snapshot_remote_ready_ck",
        ),
        CheckConstraint(
            "state <> 'DELETED' OR deleted_at IS NOT NULL",
            name="rock_snapshot_deleted_ck",
        ),
        CheckConstraint(
            "(oci_ref IS NULL AND oci_digest IS NULL AND oci_platform IS NULL "
            "AND base_image_builder_version IS NULL AND base_image_build_key IS NULL) "
            "OR (kind = 'BASE_IMAGE' AND oci_ref IS NOT NULL AND oci_digest IS NOT NULL "
            "AND oci_platform IS NOT NULL AND base_image_builder_version IS NOT NULL "
            "AND base_image_build_key IS NOT NULL)",
            name="rock_snapshot_base_image_identity_ck",
        ),
        Index("rock_snapshot_state_created_ix", "state", "created_at"),
        Index(
            "rock_snapshot_base_image_build_key_uq",
            "base_image_build_key",
            unique=True,
            # OCI imports share one canonical snapshot. E2B allocated builds
            # retain their own snapshot identity and resource/display metadata.
            postgresql_where=text(
                "kind = 'BASE_IMAGE' AND state <> 'DELETED' AND base_image_build_key IS NOT NULL "
                "AND product_metadata IS NULL"
            ),
            sqlite_where=text(
                "kind = 'BASE_IMAGE' AND state <> 'DELETED' AND base_image_build_key IS NOT NULL "
                "AND product_metadata IS NULL"
            ),
        ),
    )
