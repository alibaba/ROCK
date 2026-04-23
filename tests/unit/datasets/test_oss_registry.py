from unittest.mock import MagicMock, patch

from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.datasets.registry.oss import OssDatasetRegistry


def make_registry_info():
    return OssRegistryInfo(
        oss_bucket="test-bucket",
        oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        oss_access_key_id="key",
        oss_access_key_secret="secret",
    )


def make_list_result(prefixes=None, objects=None):
    result = MagicMock()
    result.prefix_list = prefixes or []
    result.object_list = objects or []
    return result


def test_list_datasets_returns_all():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.side_effect = [
        make_list_result(prefixes=["datasets/qwen/"]),
        make_list_result(prefixes=["datasets/qwen/my-bench/"]),
        make_list_result(prefixes=["datasets/qwen/my-bench/train/"]),
        make_list_result(prefixes=[
            "datasets/qwen/my-bench/train/task-001/",
            "datasets/qwen/my-bench/train/task-002/",
        ]),
    ]

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        datasets = registry.list_datasets()

    assert len(datasets) == 1
    assert datasets[0].id == "qwen/my-bench"
    assert datasets[0].split == "train"
    assert datasets[0].task_ids == ["task-001", "task-002"]


def test_list_datasets_filter_by_org():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.side_effect = [
        make_list_result(prefixes=["datasets/qwen/my-bench/"]),
        make_list_result(prefixes=["datasets/qwen/my-bench/train/"]),
        make_list_result(prefixes=["datasets/qwen/my-bench/train/task-001/"]),
    ]

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        datasets = registry.list_datasets(organization="qwen")

    first_call_kwargs = mock_bucket.list_objects_v2.call_args_list[0][1]
    assert first_call_kwargs["prefix"] == "datasets/qwen/"
    assert len(datasets) == 1

def test_list_datasets_counts_directory_and_file_tasks():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.side_effect = [
        make_list_result(prefixes=["datasets/qwen/"]),
        make_list_result(prefixes=["datasets/qwen/my-bench/"]),
        make_list_result(prefixes=["datasets/qwen/my-bench/train/"]),
        make_list_result(
            prefixes=["datasets/qwen/my-bench/train/task-dir/"],
            objects=[
                MagicMock(key="datasets/qwen/my-bench/train/task-file.json"),
                MagicMock(key="datasets/qwen/my-bench/train/"),
                MagicMock(key="datasets/qwen/my-bench/train/nested/task-ignored.json"),
            ],
        ),
    ]

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        datasets = registry.list_datasets()

    assert len(datasets) == 1
    assert datasets[0].id == "qwen/my-bench"
    assert datasets[0].split == "train"
    assert datasets[0].task_ids == ["task-dir", "task-file"]


def test_list_datasets_empty_registry():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        datasets = registry.list_datasets()

    assert datasets == []


def test_build_prefix_without_split():
    registry = OssDatasetRegistry(make_registry_info())
    assert registry._build_prefix("qwen", "my-bench") == "datasets/qwen/my-bench"


def test_build_prefix_with_split():
    registry = OssDatasetRegistry(make_registry_info())
    assert registry._build_prefix("qwen", "my-bench", "train") == "datasets/qwen/my-bench/train"

# ---------------------------------------------------------------------------
# list_dataset_tasks tests
# ---------------------------------------------------------------------------


def test_list_dataset_tasks_uses_default_test_split_and_sorts_task_ids():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[
        "datasets/qwen/my-bench/test/task-002/",
        "datasets/qwen/my-bench/test/task-001/",
    ])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        spec = registry.list_dataset_tasks("qwen", "my-bench")

    assert spec is not None
    assert spec.id == "qwen/my-bench"
    assert spec.split == "test"
    assert spec.task_ids == ["task-001", "task-002"]

    first_call_kwargs = mock_bucket.list_objects_v2.call_args_list[0][1]
    assert first_call_kwargs["prefix"] == "datasets/qwen/my-bench/test/"


def test_list_dataset_tasks_supports_custom_split():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[
        "datasets/qwen/my-bench/train/task-001/",
    ])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        spec = registry.list_dataset_tasks("qwen", "my-bench", "train")

    assert spec is not None
    assert spec.split == "train"
    assert spec.task_ids == ["task-001"]

    first_call_kwargs = mock_bucket.list_objects_v2.call_args_list[0][1]
    assert first_call_kwargs["prefix"] == "datasets/qwen/my-bench/train/"

def test_list_dataset_tasks_includes_directory_and_file_tasks_with_suffix_stripped():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=["datasets/qwen/my-bench/test/task-002/"],
        objects=[MagicMock(key="datasets/qwen/my-bench/test/task-001.json")],
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        spec = registry.list_dataset_tasks("qwen", "my-bench", "test")

    assert spec is not None
    assert spec.id == "qwen/my-bench"
    assert spec.split == "test"
    assert spec.task_ids == ["task-001", "task-002"]


def test_list_dataset_tasks_ignores_placeholder_and_nested_objects():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[],
        objects=[
            MagicMock(key="datasets/qwen/my-bench/test/"),
            MagicMock(key="datasets/qwen/my-bench/test/nested/task-002.json"),
            MagicMock(key="datasets/qwen/my-bench/test/task-001.json"),
        ],
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        spec = registry.list_dataset_tasks("qwen", "my-bench", "test")

    assert spec is not None
    assert spec.task_ids == ["task-001"]


def test_list_dataset_tasks_returns_none_when_no_tasks_found():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        spec = registry.list_dataset_tasks("qwen", "my-bench", "test")

    assert spec is None


# ---------------------------------------------------------------------------
# upload_dataset tests
# ---------------------------------------------------------------------------


def make_upload_pair(tmp_path, *, name="qwen/my-bench", version="train", overwrite=False):
    source = LocalDatasetConfig(path=tmp_path)
    target = RegistryDatasetConfig(
        name=name,
        version=version,
        overwrite=overwrite,
        registry=make_registry_info(),
    )
    return source, target


def test_upload_dataset_new_tasks(tmp_path):
    (tmp_path / "task-001").mkdir()
    (tmp_path / "task-001" / "task.toml").write_text("[task]")
    (tmp_path / "task-002").mkdir()
    (tmp_path / "task-002" / "task.toml").write_text("[task]")

    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(objects=[])
    source, target = make_upload_pair(tmp_path)

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        result = registry.upload_dataset(source, target)

    assert result.uploaded == 2
    assert result.skipped == 0
    assert result.failed == 0
    assert mock_bucket.put_object.call_count == 2


def test_upload_dataset_skips_existing(tmp_path):
    (tmp_path / "task-001").mkdir()
    (tmp_path / "task-001" / "task.toml").write_text("[task]")

    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        objects=[MagicMock(key="datasets/qwen/my-bench/train/task-001/task.toml")]
    )
    source, target = make_upload_pair(tmp_path, overwrite=False)

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        result = registry.upload_dataset(source, target)

    assert result.uploaded == 0
    assert result.skipped == 1
    mock_bucket.put_object.assert_not_called()


def test_upload_dataset_overwrite(tmp_path):
    (tmp_path / "task-001").mkdir()
    (tmp_path / "task-001" / "task.toml").write_text("[task]")

    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        objects=[MagicMock(key="datasets/qwen/my-bench/train/task-001/task.toml")]
    )
    source, target = make_upload_pair(tmp_path, overwrite=True)

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        result = registry.upload_dataset(source, target)

    assert result.uploaded == 1
    assert result.skipped == 0
    mock_bucket.put_object.assert_called_once()


def test_upload_dataset_oss_key_format(tmp_path):
    (tmp_path / "task-001").mkdir()
    (tmp_path / "task-001" / "task.toml").write_text("[task]")

    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(objects=[])
    source, target = make_upload_pair(tmp_path)

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        registry.upload_dataset(source, target)

    key = mock_bucket.put_object.call_args[0][0]
    assert key == "datasets/qwen/my-bench/train/task-001/task.toml"
