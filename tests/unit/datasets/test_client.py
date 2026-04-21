from unittest.mock import patch

from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.datasets.client import DatasetClient
from rock.sdk.envhub.datasets.models import DatasetSpec, UploadResult


def make_registry_info():
    return OssRegistryInfo(oss_bucket="b", oss_access_key_id="k", oss_access_key_secret="s")


def test_dataset_client_list_delegates_to_registry():
    client = DatasetClient(make_registry_info())
    expected = [DatasetSpec(id="qwen/bench", split="train", task_ids=[])]

    with patch.object(client._registry, "list_datasets", return_value=expected) as mock_list:
        result = client.list_datasets(org="qwen")

    mock_list.assert_called_once_with("qwen")
    assert result == expected


def test_dataset_client_upload_delegates_to_registry(tmp_path):
    client = DatasetClient(make_registry_info())
    source = LocalDatasetConfig(path=tmp_path)
    target = RegistryDatasetConfig(name="qwen/bench", version="train", overwrite=True, registry=make_registry_info())
    expected = UploadResult(id="qwen/bench", split="train", uploaded=1, skipped=0, failed=0)

    with patch.object(client._registry, "upload_dataset", return_value=expected) as mock_up:
        result = client.upload_dataset(source, target, concurrency=2)

    mock_up.assert_called_once_with(source, target, 2)
    assert result == expected


def test_dataset_client_list_tasks_delegates_to_registry_with_default_split():
    client = DatasetClient(make_registry_info())
    expected = DatasetSpec(id="qwen/bench", split="test", task_ids=["task-001"])

    with patch.object(client._registry, "list_dataset_tasks", return_value=expected) as mock_list_tasks:
        result = client.list_dataset_tasks("qwen", "bench")

    mock_list_tasks.assert_called_once_with("qwen", "bench", "test")
    assert result == expected
