from abc import ABC, abstractmethod

from rock.sdk.bench.models.job.config import LocalDatasetConfig, RegistryDatasetConfig
from rock.sdk.envhub.datasets.models import DatasetSpec, UploadResult


class BaseDatasetRegistry(ABC):
    @abstractmethod
    def list_datasets(self, organization: str | None = None) -> list[DatasetSpec]:
        """List all datasets. Filtered to `organization` if provided."""
        ...

    @abstractmethod
    def list_dataset_tasks(self, organization: str, dataset: str, split: str = "test") -> DatasetSpec | None:
        """List task ids for one dataset split. Returns None if dataset/split has no tasks."""
        ...

    @abstractmethod
    def list_organizations(self) -> list[str]:
        """List organization names under the dataset registry. Single backend call."""
        ...

    @abstractmethod
    def list_org_datasets(self, organization: str) -> list[str]:
        """List dataset names under one organization. Single backend call."""
        ...

    @abstractmethod
    def list_dataset_splits(self, organization: str, dataset: str) -> list[str]:
        """List split names under one dataset. Single backend call."""
        ...

    @abstractmethod
    def list_all_datasets(self, concurrency: int = 10) -> list[tuple[str, str]]:
        """List all (org, dataset) pairs. 1 + N_org backend calls with bounded concurrency."""
        ...

    @abstractmethod
    def upload_dataset(
        self,
        source: LocalDatasetConfig,
        target: RegistryDatasetConfig,
        concurrency: int = 4,
    ) -> UploadResult:
        """Upload source.path/{task_id}/ subdirs to target (org/name/split from target.name and target.version)."""
        ...
