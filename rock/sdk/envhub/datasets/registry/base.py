from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from rock.sdk.bench.models.job.config import LocalDatasetConfig, RegistryDatasetConfig
from rock.sdk.envhub.datasets.models import (
    DatasetInfo,
    DatasetSpec,
    FileEntry,
    PageResult,
    TaskEntry,
    TaskFileInfo,
    TaskInfo,
    TaskMetadata,
    UploadResult,
)


class BaseDatasetRegistry(ABC):
    # ── listing ──

    @abstractmethod
    def list_datasets(
        self, organization: str | None = None, *, offset: int = 0, limit: int | None = None
    ) -> PageResult[DatasetSpec]:
        """List all datasets. Filtered to `organization` if provided."""
        ...

    @abstractmethod
    def list_dataset_tasks(
        self,
        organization: str,
        dataset: str,
        split: str = "test",
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[str] | None:
        """List task ids for one dataset split. Returns None if dataset/split has no tasks."""
        ...

    @abstractmethod
    def list_dataset_task_entries(
        self,
        organization: str,
        dataset: str,
        split: str = "test",
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[TaskEntry] | None:
        """List task entries with type and metadata for one dataset split."""
        ...

    @abstractmethod
    def list_organizations(self, *, offset: int = 0, limit: int | None = None) -> PageResult[str]:
        """List organization names under the dataset registry."""
        ...

    @abstractmethod
    def list_org_datasets(
        self, organization: str, *, offset: int = 0, limit: int | None = None
    ) -> PageResult[str]:
        """List dataset names under one organization."""
        ...

    @abstractmethod
    def list_dataset_splits(
        self, organization: str, dataset: str, *, offset: int = 0, limit: int | None = None
    ) -> PageResult[str]:
        """List split names under one dataset."""
        ...

    @abstractmethod
    def list_all_datasets(
        self, concurrency: int = 10, *, query: str | None = None, offset: int = 0, limit: int | None = None
    ) -> PageResult[tuple[str, str]]:
        """List all (org, dataset) pairs."""
        ...

    # ── query ──

    @abstractmethod
    def get_dataset(self, organization: str, dataset: str) -> DatasetInfo | None:
        """Get dataset details: splits list and per-split task counts."""
        ...

    @abstractmethod
    def get_task(self, organization: str, dataset: str, split: str, task_id: str) -> TaskInfo | None:
        """Get task details including file metadata."""
        ...

    @abstractmethod
    def get_task_metadata(
        self, organization: str, dataset: str, split: str, task_id: str
    ) -> TaskMetadata | None:
        """Discover and return task metadata from README/metadata.json/task.toml."""
        ...

    # ── task file operations ──

    @abstractmethod
    def list_task_files(
        self, organization: str, dataset: str, split: str, task_id: str, *, offset: int = 0, limit: int | None = None
    ) -> PageResult[TaskFileInfo]:
        """List all files under a task."""
        ...

    @abstractmethod
    def browse_task_files(
        self,
        organization: str,
        dataset: str,
        split: str,
        task_id: str,
        prefix: str = "",
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[FileEntry]:
        """Browse one directory level within a task."""
        ...

    @abstractmethod
    def read_task_file(self, organization: str, dataset: str, split: str, task_id: str, file_path: str) -> bytes:
        """Read file content as bytes."""
        ...

    @abstractmethod
    def download_task_file(
        self, organization: str, dataset: str, split: str, task_id: str, file_path: str, local_path: Path
    ) -> Path:
        """Download a single file to local path. Returns the local path."""
        ...

    @abstractmethod
    def download_task(
        self, organization: str, dataset: str, split: str, task_id: str, local_dir: Path, concurrency: int = 4
    ) -> Path:
        """Download all files under a task to local directory. Returns the local directory."""
        ...

    # ── upload ──

    @abstractmethod
    def upload_dataset(
        self,
        source: LocalDatasetConfig,
        target: RegistryDatasetConfig,
        concurrency: int = 4,
    ) -> UploadResult:
        """Upload source.path/{task_id}/ subdirs to target (org/name/split from target.name and target.version)."""
        ...
