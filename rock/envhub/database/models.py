from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from rock.envhub.database.base import Base


class Dataset(Base):
    __tablename__ = "datasets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    org = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    tags = Column(JSON, default=list)
    owner = Column(String(255), default="")
    homepage = Column(String(512), nullable=True)
    repo = Column(String(512), nullable=True)
    paper = Column(String(512), nullable=True)
    leaderboard = Column(String(512), nullable=True)
    logo_url = Column(String(512), nullable=True)
    os = Column(String(64), nullable=True)
    version = Column(String(64), nullable=True)
    task_counts = Column(JSON, nullable=True, default=dict)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    instances = relationship("Instance", back_populates="dataset", cascade="all, delete-orphan")
    permissions = relationship("DatasetPermission", back_populates="dataset", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("org", "name", name="uq_dataset_org_name"),)

    @property
    def full_name(self) -> str:
        return f"{self.org}/{self.name}"

    def __repr__(self):
        return f"<Dataset(id={self.id}, name='{self.full_name}')>"


class Instance(Base):
    __tablename__ = "instances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    split = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    type = Column(String(16), default="directory")
    size = Column(BigInteger, nullable=True)
    file_count = Column(Integer, nullable=True)
    etag = Column(String(255), nullable=True)
    format = Column(String(64), nullable=True, index=True)
    repo = Column(String(512), nullable=True)
    language = Column(String(64), nullable=True, index=True)
    difficulty = Column(String(64), nullable=True)
    base_commit = Column(String(64), nullable=True)
    image_uris = Column(JSON, nullable=True)
    raw = Column(Text, nullable=True)
    source_revision = Column(String(128), nullable=True)
    imported_from = Column(String(512), nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    dataset = relationship("Dataset", back_populates="instances")

    __table_args__ = (
        UniqueConstraint("dataset_id", "split", "name", name="uq_instance_dataset_split_name"),
        Index("ix_instance_dataset_split", "dataset_id", "split"),
    )

    def __repr__(self):
        return f"<Instance(id={self.id}, name='{self.name}', split='{self.split}')>"


class Image(Base):
    __tablename__ = "images"

    source_image_uri = Column(String(512), primary_key=True)
    image_uri_sg = Column(String(512), nullable=True)
    image_uri_sh = Column(String(512), nullable=True)
    image_hash = Column(String(71), nullable=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    last_error = Column(Text, nullable=True)
    last_job_id = Column(String(64), nullable=True)
    created_by = Column(String(255), nullable=False, default="system")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<Image(source_image_uri='{self.source_image_uri}', status='{self.status}')>"


class DatasetPermission(Base):
    __tablename__ = "dataset_permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(255), nullable=False)
    role = Column(String(32), nullable=False, default="viewer")
    granted_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    dataset = relationship("Dataset", back_populates="permissions")

    __table_args__ = (
        UniqueConstraint("dataset_id", "user_id", name="uq_permission_dataset_user"),
        Index("ix_permission_user", "user_id"),
    )

    def __repr__(self):
        return f"<DatasetPermission(dataset_id={self.dataset_id}, user_id='{self.user_id}', role='{self.role}')>"


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_type = Column(String(32), nullable=False, index=True)
    target_id = Column(String(512), nullable=False)
    event_type = Column(String(64), nullable=False, index=True)
    operator = Column(String(255), nullable=False)
    changes = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_audit_target", "target_type", "target_id"),)

    def __repr__(self):
        return f"<AuditEvent(id={self.id}, target_type='{self.target_type}', event_type='{self.event_type}')>"
