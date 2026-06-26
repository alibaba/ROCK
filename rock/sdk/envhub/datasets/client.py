from __future__ import annotations

from pathlib import Path

from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
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
from rock.sdk.envhub.datasets.registry.oss import OssDatasetRegistry


class DatasetClient:
    def __init__(self, registry: OssRegistryInfo) -> None:
        self._registry = OssDatasetRegistry(registry)

    # ── listing ──

    def list_datasets(
        self, org: str | None = None, *, offset: int = 0, limit: int | None = None
    ) -> PageResult[DatasetSpec]:
        return self._registry.list_datasets(org, offset=offset, limit=limit)

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
        return self._registry.list_dataset_tasks(organization, dataset, split, query=query, offset=offset, limit=limit)

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
        return self._registry.list_dataset_task_entries(
            organization, dataset, split, query=query, offset=offset, limit=limit
        )

    def list_organizations(self, *, offset: int = 0, limit: int | None = None) -> PageResult[str]:
        return self._registry.list_organizations(offset=offset, limit=limit)

    def list_org_datasets(self, organization: str, *, offset: int = 0, limit: int | None = None) -> PageResult[str]:
        return self._registry.list_org_datasets(organization, offset=offset, limit=limit)

    def list_all_datasets(
        self, concurrency: int = 10, *, query: str | None = None, offset: int = 0, limit: int | None = None
    ) -> PageResult[tuple[str, str]]:
        return self._registry.list_all_datasets(concurrency, query=query, offset=offset, limit=limit)

    def list_dataset_splits(
        self, organization: str, dataset: str, *, offset: int = 0, limit: int | None = None
    ) -> PageResult[str]:
        return self._registry.list_dataset_splits(organization, dataset, offset=offset, limit=limit)

    # ── query ──

    def get_dataset(self, organization: str, dataset: str) -> DatasetInfo | None:
        return self._registry.get_dataset(organization, dataset)

    def get_task(self, organization: str, dataset: str, split: str, task_id: str) -> TaskInfo | None:
        return self._registry.get_task(organization, dataset, split, task_id)

    def get_task_metadata(self, organization: str, dataset: str, split: str, task_id: str) -> TaskMetadata | None:
        return self._registry.get_task_metadata(organization, dataset, split, task_id)

    # ── task file operations ──

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
        return self._registry.browse_task_files(
            organization, dataset, split, task_id, prefix, offset=offset, limit=limit
        )

    def list_task_files(
        self, organization: str, dataset: str, split: str, task_id: str, *, offset: int = 0, limit: int | None = None
    ) -> PageResult[TaskFileInfo]:
        return self._registry.list_task_files(organization, dataset, split, task_id, offset=offset, limit=limit)

    def read_task_file(self, organization: str, dataset: str, split: str, task_id: str, file_path: str) -> bytes:
        return self._registry.read_task_file(organization, dataset, split, task_id, file_path)

    def download_task_file(
        self, organization: str, dataset: str, split: str, task_id: str, file_path: str, local_path: Path
    ) -> Path:
        return self._registry.download_task_file(organization, dataset, split, task_id, file_path, local_path)

    def download_task(
        self, organization: str, dataset: str, split: str, task_id: str, local_dir: Path, concurrency: int = 4
    ) -> Path:
        return self._registry.download_task(organization, dataset, split, task_id, local_dir, concurrency)

    # ── metadata ──

    def refresh_metadata(
        self, organization: str, dataset: str, split: str | None = None, concurrency: int = 4
    ) -> dict:
        return self._registry.refresh_metadata(organization, dataset, split=split, concurrency=concurrency)

    # ── upload ──

    def upload_dataset(
        self,
        source: LocalDatasetConfig,
        target: RegistryDatasetConfig,
        concurrency: int = 4,
    ) -> UploadResult:
        return self._registry.upload_dataset(source, target, concurrency)

    # ── sync ──

    def sync_dataset(
        self,
        dataset: str,
        target: OssRegistryInfo,
        *,
        split: str | None = None,
        dry_run: bool = True,
        delete_extra: bool = False,
    ):
        return self._registry.sync_dataset(dataset, target, split=split, dry_run=dry_run, delete_extra=delete_extra)

    # ── reserved interfaces (not yet implemented) ──

    def transfer_images(self, **kwargs) -> None:
        raise NotImplementedError("Image transfer is not yet implemented in the dataset client.")

    def audit_dataset(self, **kwargs) -> None:
        raise NotImplementedError("Dataset audit is not yet implemented in the dataset client.")
