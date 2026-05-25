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
        make_list_result(
            prefixes=[
                "datasets/qwen/my-bench/train/task-001/",
                "datasets/qwen/my-bench/train/task-002/",
            ]
        ),
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
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[
            "datasets/qwen/my-bench/test/task-002/",
            "datasets/qwen/my-bench/test/task-001/",
        ]
    )

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
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[
            "datasets/qwen/my-bench/train/task-001/",
        ]
    )

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


# ---------------------------------------------------------------------------
# list_organizations tests
# ---------------------------------------------------------------------------


def test_list_organizations_returns_sorted_org_names():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[
            "datasets/qwen/",
            "datasets/alibaba/",
            "datasets/AoneBenchDev/",
        ]
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        orgs = registry.list_organizations()

    call_kwargs = mock_bucket.list_objects_v2.call_args[1]
    assert call_kwargs["prefix"] == "datasets/"
    assert call_kwargs["delimiter"] == "/"
    assert call_kwargs["max_keys"] == 1000
    assert orgs == ["AoneBenchDev", "alibaba", "qwen"]


def test_list_organizations_returns_empty_when_no_orgs():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        orgs = registry.list_organizations()

    assert orgs == []


def test_list_org_datasets_returns_sorted_dataset_names():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[
            "datasets/qwen/bench-2/",
            "datasets/qwen/bench-1/",
        ]
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        datasets = registry.list_org_datasets("qwen")

    call_kwargs = mock_bucket.list_objects_v2.call_args[1]
    assert call_kwargs["prefix"] == "datasets/qwen/"
    assert call_kwargs["delimiter"] == "/"
    assert call_kwargs["max_keys"] == 1000
    assert datasets == ["bench-1", "bench-2"]


def test_list_org_datasets_returns_empty_when_org_missing():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        assert registry.list_org_datasets("nonexistent") == []


def test_list_dataset_splits_returns_sorted_split_names():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[
            "datasets/qwen/bench/train/",
            "datasets/qwen/bench/test/",
        ]
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        splits = registry.list_dataset_splits("qwen", "bench")

    call_kwargs = mock_bucket.list_objects_v2.call_args[1]
    assert call_kwargs["prefix"] == "datasets/qwen/bench/"
    assert call_kwargs["delimiter"] == "/"
    assert splits == ["test", "train"]


def test_list_dataset_splits_returns_empty_when_dataset_missing():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        assert registry.list_dataset_splits("qwen", "nope") == []


def test_list_all_datasets_returns_sorted_pairs():
    registry = OssDatasetRegistry(make_registry_info())

    def fake_list_org_datasets(org):
        return {"qwen": ["bench-2", "bench-1"], "alibaba": ["pinch"]}[org]

    with patch.object(registry, "list_organizations", return_value=["qwen", "alibaba"]):
        with patch.object(registry, "list_org_datasets", side_effect=fake_list_org_datasets):
            pairs = registry.list_all_datasets()

    assert pairs == [("alibaba", "pinch"), ("qwen", "bench-1"), ("qwen", "bench-2")]


def test_list_all_datasets_uses_bounded_concurrency():
    registry = OssDatasetRegistry(make_registry_info())

    with patch.object(registry, "list_organizations", return_value=["o1", "o2"]):
        with patch.object(registry, "list_org_datasets", return_value=["d"]):
            with patch("rock.sdk.envhub.datasets.registry.oss.ThreadPoolExecutor") as mock_pool:
                with patch("rock.sdk.envhub.datasets.registry.oss.as_completed", side_effect=lambda d: list(d)):
                    mock_executor = MagicMock()
                    mock_pool.return_value.__enter__.return_value = mock_executor
                    future = MagicMock()
                    future.result.return_value = ["d"]
                    mock_executor.submit.return_value = future
                    registry.list_all_datasets(concurrency=7)

    mock_pool.assert_called_once_with(max_workers=7)


def test_list_all_datasets_default_concurrency_is_10():
    registry = OssDatasetRegistry(make_registry_info())

    with patch.object(registry, "list_organizations", return_value=["o1"]):
        with patch.object(registry, "list_org_datasets", return_value=["d"]):
            with patch("rock.sdk.envhub.datasets.registry.oss.ThreadPoolExecutor") as mock_pool:
                with patch("rock.sdk.envhub.datasets.registry.oss.as_completed", side_effect=lambda d: list(d)):
                    mock_executor = MagicMock()
                    mock_pool.return_value.__enter__.return_value = mock_executor
                    future = MagicMock()
                    future.result.return_value = ["d"]
                    mock_executor.submit.return_value = future
                    registry.list_all_datasets()

    mock_pool.assert_called_once_with(max_workers=10)


def test_list_all_datasets_propagates_exception_from_worker():
    import pytest as _pytest

    registry = OssDatasetRegistry(make_registry_info())

    def fake_list_org_datasets(org):
        if org == "bad":
            raise RuntimeError("oss boom")
        return ["d"]

    with patch.object(registry, "list_organizations", return_value=["good", "bad"]):
        with patch.object(registry, "list_org_datasets", side_effect=fake_list_org_datasets):
            with _pytest.raises(RuntimeError, match="oss boom"):
                registry.list_all_datasets()


def test_list_all_datasets_empty_when_no_orgs():
    registry = OssDatasetRegistry(make_registry_info())
    with patch.object(registry, "list_organizations", return_value=[]):
        assert registry.list_all_datasets() == []
