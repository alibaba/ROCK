from unittest.mock import patch

import pytest

from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.datasets.client import DatasetClient
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


def make_registry_info():
    return OssRegistryInfo(oss_bucket="b", oss_access_key_id="k", oss_access_key_secret="s")


def test_dataset_client_list_delegates_to_registry():
    client = DatasetClient(make_registry_info())
    expected = PageResult(items=[DatasetSpec(id="qwen/bench", split="train", task_ids=[])], total=1, offset=0, limit=None)

    with patch.object(client._registry, "list_datasets", return_value=expected) as mock_list:
        result = client.list_datasets(org="qwen")

    mock_list.assert_called_once_with("qwen", offset=0, limit=None)
    assert result == expected


def test_dataset_client_list_with_pagination():
    client = DatasetClient(make_registry_info())
    expected = PageResult(items=[DatasetSpec(id="qwen/bench", split="train", task_ids=[])], total=5, offset=2, limit=1)

    with patch.object(client._registry, "list_datasets", return_value=expected) as mock_list:
        result = client.list_datasets(org="qwen", offset=2, limit=1)

    mock_list.assert_called_once_with("qwen", offset=2, limit=1)
    assert result.total == 5
    assert result.offset == 2
    assert len(result.items) == 1


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
    expected = PageResult(items=["task-001"], total=1, offset=0, limit=None)

    with patch.object(client._registry, "list_dataset_tasks", return_value=expected) as mock_list_tasks:
        result = client.list_dataset_tasks("qwen", "bench")

    mock_list_tasks.assert_called_once_with("qwen", "bench", "test", query=None, offset=0, limit=None)
    assert result == expected


def test_dataset_client_list_tasks_with_pagination():
    client = DatasetClient(make_registry_info())
    expected = PageResult(items=["task-003", "task-004"], total=10, offset=2, limit=2)

    with patch.object(client._registry, "list_dataset_tasks", return_value=expected) as m:
        result = client.list_dataset_tasks("qwen", "bench", "test", offset=2, limit=2)

    m.assert_called_once_with("qwen", "bench", "test", query=None, offset=2, limit=2)
    assert result.total == 10
    assert result.offset == 2
    assert len(result.items) == 2


def test_dataset_client_list_organizations_delegates():
    client = DatasetClient(make_registry_info())
    expected = PageResult(items=["a", "b"], total=2, offset=0, limit=None)
    with patch.object(client._registry, "list_organizations", return_value=expected) as m:
        result = client.list_organizations()
    m.assert_called_once_with(offset=0, limit=None)
    assert result.items == ["a", "b"]


def test_dataset_client_list_org_datasets_delegates():
    client = DatasetClient(make_registry_info())
    expected = PageResult(items=["d1"], total=1, offset=0, limit=None)
    with patch.object(client._registry, "list_org_datasets", return_value=expected) as m:
        result = client.list_org_datasets("qwen")
    m.assert_called_once_with("qwen", offset=0, limit=None)
    assert result.items == ["d1"]


def test_dataset_client_list_all_datasets_delegates_with_default_concurrency():
    client = DatasetClient(make_registry_info())
    expected = PageResult(items=[("a", "x")], total=1, offset=0, limit=None)
    with patch.object(client._registry, "list_all_datasets", return_value=expected) as m:
        result = client.list_all_datasets()
    m.assert_called_once_with(10, query=None, offset=0, limit=None)
    assert result.items == [("a", "x")]


def test_dataset_client_list_all_datasets_passes_custom_concurrency():
    client = DatasetClient(make_registry_info())
    expected = PageResult(items=[], total=0, offset=0, limit=None)
    with patch.object(client._registry, "list_all_datasets", return_value=expected) as m:
        client.list_all_datasets(concurrency=5)
    m.assert_called_once_with(5, query=None, offset=0, limit=None)


def test_dataset_client_list_dataset_splits_delegates():
    client = DatasetClient(make_registry_info())
    expected = PageResult(items=["test", "train"], total=2, offset=0, limit=None)
    with patch.object(client._registry, "list_dataset_splits", return_value=expected) as m:
        result = client.list_dataset_splits("qwen", "bench")
    m.assert_called_once_with("qwen", "bench", offset=0, limit=None)
    assert result.items == ["test", "train"]


def test_get_dataset_delegates():
    client = DatasetClient(make_registry_info())
    expected = DatasetInfo(id="qwen/bench", splits=["test", "train"], task_counts={"test": 10, "train": 50})
    with patch.object(client._registry, "get_dataset", return_value=expected) as m:
        result = client.get_dataset("qwen", "bench")
    m.assert_called_once_with("qwen", "bench")
    assert result == expected


def test_get_dataset_returns_none():
    client = DatasetClient(make_registry_info())
    with patch.object(client._registry, "get_dataset", return_value=None) as m:
        result = client.get_dataset("qwen", "nonexistent")
    m.assert_called_once_with("qwen", "nonexistent")
    assert result is None


def test_get_task_delegates():
    client = DatasetClient(make_registry_info())
    files = [TaskFileInfo(path="data.json", size=1024, last_modified="2025-01-01T00:00:00+00:00")]
    expected = TaskInfo(task_id="task-001", dataset_id="qwen/bench", split="test", files=files, total_size=1024)
    with patch.object(client._registry, "get_task", return_value=expected) as m:
        result = client.get_task("qwen", "bench", "test", "task-001")
    m.assert_called_once_with("qwen", "bench", "test", "task-001")
    assert result == expected
    assert result.total_size == 1024


def test_list_task_files_delegates():
    client = DatasetClient(make_registry_info())
    files = [
        TaskFileInfo(path="a.txt", size=100, last_modified="2025-01-01T00:00:00+00:00"),
        TaskFileInfo(path="b.txt", size=200, last_modified="2025-01-02T00:00:00+00:00"),
    ]
    expected = PageResult(items=files, total=2, offset=0, limit=None)
    with patch.object(client._registry, "list_task_files", return_value=expected) as m:
        result = client.list_task_files("qwen", "bench", "test", "task-001")
    m.assert_called_once_with("qwen", "bench", "test", "task-001", offset=0, limit=None)
    assert result.items == files


def test_list_task_files_with_pagination():
    client = DatasetClient(make_registry_info())
    files = [TaskFileInfo(path="c.txt", size=300, last_modified="2025-01-03T00:00:00+00:00")]
    expected = PageResult(items=files, total=10, offset=5, limit=1)
    with patch.object(client._registry, "list_task_files", return_value=expected) as m:
        result = client.list_task_files("qwen", "bench", "test", "task-001", offset=5, limit=1)
    m.assert_called_once_with("qwen", "bench", "test", "task-001", offset=5, limit=1)
    assert result.total == 10
    assert result.offset == 5


def test_read_task_file_delegates():
    client = DatasetClient(make_registry_info())
    expected = b"file content here"
    with patch.object(client._registry, "read_task_file", return_value=expected) as m:
        result = client.read_task_file("qwen", "bench", "test", "task-001", "data.json")
    m.assert_called_once_with("qwen", "bench", "test", "task-001", "data.json")
    assert result == expected


def test_download_task_file_delegates(tmp_path):
    client = DatasetClient(make_registry_info())
    local = tmp_path / "data.json"
    with patch.object(client._registry, "download_task_file", return_value=local) as m:
        result = client.download_task_file("qwen", "bench", "test", "task-001", "data.json", local)
    m.assert_called_once_with("qwen", "bench", "test", "task-001", "data.json", local)
    assert result == local


def test_download_task_delegates(tmp_path):
    client = DatasetClient(make_registry_info())
    task_dir = tmp_path / "task-001"
    with patch.object(client._registry, "download_task", return_value=task_dir) as m:
        result = client.download_task("qwen", "bench", "test", "task-001", tmp_path, concurrency=2)
    m.assert_called_once_with("qwen", "bench", "test", "task-001", tmp_path, 2)
    assert result == task_dir


def test_transfer_images_not_implemented():
    client = DatasetClient(make_registry_info())
    with pytest.raises(NotImplementedError):
        client.transfer_images()


def test_audit_dataset_not_implemented():
    client = DatasetClient(make_registry_info())
    with pytest.raises(NotImplementedError):
        client.audit_dataset()


def test_list_dataset_task_entries_delegates():
    client = DatasetClient(make_registry_info())
    entry = TaskEntry(name="t1", path="t1", type="directory")
    expected = PageResult(items=[entry], total=1, offset=0, limit=None)
    with patch.object(client._registry, "list_dataset_task_entries", return_value=expected) as m:
        result = client.list_dataset_task_entries("qwen", "bench", "test", query="t1", offset=0, limit=10)
    m.assert_called_once_with("qwen", "bench", "test", query="t1", offset=0, limit=10)
    assert result == expected


def test_list_dataset_tasks_with_query_delegates():
    client = DatasetClient(make_registry_info())
    expected = PageResult(items=["dask__dask-001"], total=1, offset=0, limit=None)
    with patch.object(client._registry, "list_dataset_tasks", return_value=expected) as m:
        result = client.list_dataset_tasks("qwen", "bench", "test", query="dask")
    m.assert_called_once_with("qwen", "bench", "test", query="dask", offset=0, limit=None)
    assert result == expected


def test_browse_task_files_delegates():
    client = DatasetClient(make_registry_info())
    entry = FileEntry(name="README.md", path="README.md", type="file", size=100)
    expected = PageResult(items=[entry], total=1, offset=0, limit=None)
    with patch.object(client._registry, "browse_task_files", return_value=expected) as m:
        result = client.browse_task_files("qwen", "bench", "test", "task-1", "data", offset=0, limit=10)
    m.assert_called_once_with("qwen", "bench", "test", "task-1", "data", offset=0, limit=10)
    assert result == expected


def test_get_task_metadata_delegates():
    client = DatasetClient(make_registry_info())
    expected = TaskMetadata(source="README.md", format="markdown", content="# Hello")
    with patch.object(client._registry, "get_task_metadata", return_value=expected) as m:
        result = client.get_task_metadata("qwen", "bench", "test", "task-1")
    m.assert_called_once_with("qwen", "bench", "test", "task-1")
    assert result == expected


def test_list_all_datasets_with_query_delegates():
    client = DatasetClient(make_registry_info())
    expected = PageResult(items=[("alibaba", "pinch")], total=1, offset=0, limit=None)
    with patch.object(client._registry, "list_all_datasets", return_value=expected) as m:
        result = client.list_all_datasets(query="pinch")
    m.assert_called_once_with(10, query="pinch", offset=0, limit=None)
    assert result == expected
