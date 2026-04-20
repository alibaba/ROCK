from abc import ABC, abstractmethod

from rock.sdk.bench.models.job.config import LocalDatasetConfig, RegistryDatasetConfig
from rock.sdk.envhub.datasets.models import DatasetSpec, UploadResult


class BaseDatasetRegistry(ABC):

    @abstractmethod
    def list_datasets(self, organization: str | None = None) -> list[DatasetSpec]:
        """List all datasets. Filtered to `organization` if provided."""
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
