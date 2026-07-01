from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass
class PageResult(Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int | None


@dataclass
class DatasetSpec:
    id: str  # "{organization}/{dataset_name}", e.g. "princeton-nlp/SWE-bench_Verified"
    split: str
    task_ids: list[str] = field(default_factory=list)


@dataclass
class DatasetInfo:
    id: str  # "org/dataset"
    description: str = ""
    tags: list[str] = field(default_factory=list)
    owner: str = ""
    homepage: str | None = None
    repo: str | None = None
    paper: str | None = None
    leaderboard: str | None = None
    logo_url: str | None = None
    os: str | None = None
    version: str | None = None
    splits: list[str] = field(default_factory=list)
    task_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class TaskFileInfo:
    path: str
    size: int
    last_modified: str


@dataclass
class TaskInfo:
    task_id: str
    dataset_id: str  # "org/dataset"
    split: str
    files: list[TaskFileInfo] = field(default_factory=list)
    total_size: int = 0


@dataclass
class UploadResult:
    id: str  # "{organization}/{dataset_name}"
    split: str
    uploaded: int
    skipped: int
    failed: int


@dataclass
class TaskEntry:
    name: str
    path: str
    type: str  # "file" or "directory"
    size: int | None = None
    file_count: int | None = None
    etag: str | None = None
    description: str = ""
    format: str | None = None
    repo: str | None = None
    language: str | None = None
    difficulty: str | None = None
    base_commit: str | None = None
    image_uris: list[str] | None = None
    raw: str | None = None
    source_revision: str | None = None
    imported_from: str | None = None
    created_by: str | None = None
    updated_at: str | None = None


@dataclass
class FileEntry:
    name: str
    path: str
    type: str  # "file" or "directory"
    size: int | None = None
    media_type: str | None = None
    updated_at: str | None = None
    etag: str | None = None


@dataclass
class TaskMetadata:
    source: str
    format: str  # "markdown", "json", "toml", "text"
    content: str
    parsed: Any = None
    generated: bool = False


@dataclass
class ImageInfo:
    source_image_uri: str
    image_uri_sg: str | None = None
    image_uri_sh: str | None = None
    image_hash: str | None = None
    status: str = "pending"
    last_error: str | None = None
    last_job_id: str | None = None
    created_by: str = "system"
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class PermissionInfo:
    dataset_id: str
    user_id: str
    role: str = "viewer"
    granted_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class AuditEventInfo:
    id: int
    target_type: str
    target_id: str
    event_type: str
    operator: str
    changes: dict | None = None
    created_at: str | None = None
