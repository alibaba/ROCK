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
    updated_at: str | None = None
    etag: str | None = None


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
