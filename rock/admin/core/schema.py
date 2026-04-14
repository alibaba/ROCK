"""ORM base and models for the ROCK admin database.

``Base`` is the single SQLAlchemy ``DeclarativeBase`` for all ROCK tables.
``SandboxRecord`` is the canonical persistence model for sandbox metadata.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import Boolean, Column, Float, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import JSON


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
    host_name = Column(String(255), nullable=True)
    auth_token = Column(String(512), nullable=True)
    rock_authorization_encrypted = Column(String(1024), nullable=True)
    cpus = Column(Float, nullable=True)
    memory = Column(String(64), nullable=True)
    create_user_gray_flag = Column(Boolean, nullable=True)
    phases = Column(_JSONB_VARIANT, nullable=True)
    port_mapping = Column(_JSONB_VARIANT, nullable=True)
    spec = Column(_JSONB_VARIANT, nullable=True)
    status = Column(_JSONB_VARIANT, nullable=True)

    __table_args__ = (
        Index("ix_sandbox_record_user_id", "user_id"),
        Index("ix_sandbox_record_state", "state"),
        Index("ix_sandbox_record_namespace", "namespace"),
        Index("ix_sandbox_record_experiment_id", "experiment_id"),
        Index("ix_sandbox_record_cluster_name", "cluster_name"),
        Index("ix_sandbox_record_image", "image"),
        Index("ix_sandbox_record_host_ip", "host_ip"),
        Index("ix_sandbox_record_host_name", "host_name"),
        Index("ix_sandbox_record_create_user_gray_flag", "create_user_gray_flag"),
    )

    # Columns allowed as the filter key in list_by().
    # Only include columns with an index (or PK); exclude JSONB, sensitive, and internal columns.
    LIST_BY_ALLOWLIST: ClassVar[frozenset[str]] = frozenset(
        {
            "sandbox_id",
            "user_id",
            "image",
            "experiment_id",
            "namespace",
            "cluster_name",
            "state",
            "host_ip",
            "host_name",
            "create_user_gray_flag",
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
    }

    def to_dict(self) -> dict[str, Any]:
        """Return all non-``None`` column values as a plain dict."""
        return {c.key: getattr(self, c.key) for c in self.__table__.columns if getattr(self, c.key) is not None}
