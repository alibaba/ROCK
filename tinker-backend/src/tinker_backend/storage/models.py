"""SQLAlchemy ORM models for Tinker backend persistence."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Index, JSON, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tinker_backend.protocol.enums import (
    ActionKind,
    ActionState,
    EnvironmentState,
    FutureState,
    RuntimeState,
)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ClientSessionRecord(Base):
    __tablename__ = "client_sessions"

    session_id: Mapped[str] = mapped_column(primary_key=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    user_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sdk_version: Mapped[str] = mapped_column(default="unknown")
    project_id: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, index=True)
    heartbeat_count: Mapped[int] = mapped_column(default=0)


class RuntimeInstanceRecord(Base):
    __tablename__ = "runtime_instances"

    runtime_id: Mapped[str] = mapped_column(primary_key=True)
    runtime_type: Mapped[str] = mapped_column(index=True)
    config_type: Mapped[str] = mapped_column(default="yaml")
    config_content: Mapped[str] = mapped_column(Text)
    config_path: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default=RuntimeState.CREATING.value, index=True)
    ready: Mapped[bool] = mapped_column(default=False, index=True)
    session_id: Mapped[str | None] = mapped_column(nullable=True, index=True)
    adapter_base_url: Mapped[str | None] = mapped_column(default=None)
    launcher_owner: Mapped[str | None] = mapped_column(default=None)
    process_pid: Mapped[int | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, index=True)


class OperationFutureRecord(Base):
    __tablename__ = "operation_futures"
    __table_args__ = (
        Index("ix_operation_futures_type_status", "request_type", "status"),
    )

    request_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    request_type: Mapped[str] = mapped_column(index=True)
    runtime_id: Mapped[str | None] = mapped_column(nullable=True, index=True)
    model_id: Mapped[str | None] = mapped_column(default=None, index=True)
    request_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    status: Mapped[str] = mapped_column(default=FutureState.PENDING.value, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class RuntimeActionRecord(Base):
    __tablename__ = "runtime_actions"
    __table_args__ = (
        Index("ix_runtime_actions_runtime_status", "runtime_id", "status"),
    )

    action_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    runtime_id: Mapped[str] = mapped_column(index=True)
    action_type: Mapped[str] = mapped_column(index=True)
    env_id: Mapped[str | None] = mapped_column(nullable=True, index=True)
    future_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(default=ActionState.PENDING.value, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    result_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskEnvironmentRecord(Base):
    __tablename__ = "task_environments"

    env_id: Mapped[str] = mapped_column(primary_key=True)
    runtime_id: Mapped[str] = mapped_column(index=True)
    task_id: Mapped[str] = mapped_column(default="")
    dataset: Mapped[str] = mapped_column(default="")
    split: Mapped[str] = mapped_column(default="")
    status: Mapped[str] = mapped_column(default=EnvironmentState.PENDING_INIT.value, index=True)
    env_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EnvironmentStepRecord(Base):
    __tablename__ = "environment_steps"

    env_id: Mapped[str] = mapped_column(primary_key=True)
    step_id: Mapped[int] = mapped_column(primary_key=True)
    prompt: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    finish_reason: Mapped[str | None] = mapped_column(default=None)
    reward: Mapped[float | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrainingModelRecord(Base):
    __tablename__ = "training_models"
    __table_args__ = (
        Index("ix_training_models_runtime_status", "runtime_id", "status"),
        Index("ix_training_models_session_seq", "session_id", "model_seq_id"),
    )

    model_id: Mapped[str] = mapped_column(primary_key=True)
    runtime_id: Mapped[str] = mapped_column(index=True)
    session_id: Mapped[str | None] = mapped_column(nullable=True, index=True)
    model_seq_id: Mapped[int | None] = mapped_column(default=None)
    base_model: Mapped[str] = mapped_column(index=True)
    lora_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    user_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    status: Mapped[str] = mapped_column(default="pending_create", index=True)
    create_future_id: Mapped[int | None] = mapped_column(default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SamplingSessionRecord(Base):
    __tablename__ = "sampling_sessions"
    __table_args__ = (
        Index("ix_sampling_sessions_runtime", "runtime_id"),
        Index("ix_sampling_sessions_session_seq", "session_id", "sampling_session_seq_id"),
    )

    sampling_session_id: Mapped[str] = mapped_column(primary_key=True)
    runtime_id: Mapped[str] = mapped_column(index=True)
    session_id: Mapped[str | None] = mapped_column(nullable=True, index=True)
    sampling_session_seq_id: Mapped[int | None] = mapped_column(default=None)
    base_model: Mapped[str | None] = mapped_column(default=None)
    model_path: Mapped[str | None] = mapped_column(default=None)
    model_id: Mapped[str | None] = mapped_column(default=None, index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CheckpointRecord(Base):
    __tablename__ = "checkpoints"
    __table_args__ = (
        Index("ix_checkpoints_runtime_model", "runtime_id", "model_id"),
    )

    checkpoint_id: Mapped[str] = mapped_column(primary_key=True)
    model_id: Mapped[str] = mapped_column(primary_key=True)
    checkpoint_type: Mapped[str] = mapped_column(primary_key=True)
    runtime_id: Mapped[str] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(default="pending", index=True)
    path: Mapped[str | None] = mapped_column(default=None)
    future_id: Mapped[int | None] = mapped_column(default=None, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
