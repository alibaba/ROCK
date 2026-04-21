from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.datasets.models import DatasetSpec, UploadResult
from rock.sdk.envhub.datasets.registry.oss import OssDatasetRegistry


class DatasetClient:

    def __init__(self, registry: OssRegistryInfo) -> None:
        self._registry = OssDatasetRegistry(registry)

    def list_datasets(self, org: str | None = None) -> list[DatasetSpec]:
        return self._registry.list_datasets(org)

    def list_dataset_tasks(self, organization: str, dataset: str, split: str = "test") -> DatasetSpec | None:
        return self._registry.list_dataset_tasks(organization, dataset, split)

    def upload_dataset(
        self,
        source: LocalDatasetConfig,
        target: RegistryDatasetConfig,
        concurrency: int = 4,
    ) -> UploadResult:
        return self._registry.upload_dataset(source, target, concurrency)
