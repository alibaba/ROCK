from unittest.mock import MagicMock, patch

import oss2.exceptions

from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.datasets.models import PageResult
from rock.sdk.envhub.datasets.registry.oss import OssDatasetRegistry


def make_registry_info():
    return OssRegistryInfo(
        oss_bucket="test-bucket",
        oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        oss_access_key_id="key",
        oss_access_key_secret="secret",
    )


def make_list_result(prefixes=None, objects=None, is_truncated=False):
    result = MagicMock()
    result.prefix_list = prefixes or []
    result.object_list = objects or []
    result.is_truncated = is_truncated
    result.next_continuation_token = ""
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
        page = registry.list_datasets()

    assert len(page.items) == 1
    assert page.items[0].id == "qwen/my-bench"
    assert page.items[0].split == "train"
    assert page.items[0].task_ids == ["task-001", "task-002"]


def test_list_datasets_filter_by_org():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.side_effect = [
        make_list_result(prefixes=["datasets/qwen/my-bench/"]),
        make_list_result(prefixes=["datasets/qwen/my-bench/train/"]),
        make_list_result(prefixes=["datasets/qwen/my-bench/train/task-001/"]),
    ]

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_datasets(organization="qwen")

    first_call_kwargs = mock_bucket.list_objects_v2.call_args_list[0][1]
    assert first_call_kwargs["prefix"] == "datasets/qwen/"
    assert len(page.items) == 1


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
        page = registry.list_datasets()

    assert len(page.items) == 1
    assert page.items[0].id == "qwen/my-bench"
    assert page.items[0].split == "train"
    assert page.items[0].task_ids == ["task-dir", "task-file"]


def test_list_datasets_empty_registry():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_datasets()

    assert page.items == []
    assert page.total == 0


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
        page = registry.list_dataset_tasks("qwen", "my-bench")

    assert page is not None
    assert page.items == ["task-001", "task-002"]
    assert page.total == 2

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
        page = registry.list_dataset_tasks("qwen", "my-bench", "train")

    assert page is not None
    assert page.items == ["task-001"]

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
        page = registry.list_dataset_tasks("qwen", "my-bench", "test")

    assert page is not None
    assert page.items == ["task-001", "task-002"]


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
        page = registry.list_dataset_tasks("qwen", "my-bench", "test")

    assert page is not None
    assert page.items == ["task-001"]


def test_list_dataset_tasks_returns_none_when_no_tasks_found():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_dataset_tasks("qwen", "my-bench", "test")

    assert page is None


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
        page = registry.list_organizations()

    call_kwargs = mock_bucket.list_objects_v2.call_args[1]
    assert call_kwargs["prefix"] == "datasets/"
    assert call_kwargs["delimiter"] == "/"
    assert call_kwargs["max_keys"] == 1000
    assert page.items == ["AoneBenchDev", "alibaba", "qwen"]
    assert page.total == 3


def test_list_organizations_returns_empty_when_no_orgs():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_organizations()

    assert page.items == []
    assert page.total == 0


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
        page = registry.list_org_datasets("qwen")

    call_kwargs = mock_bucket.list_objects_v2.call_args[1]
    assert call_kwargs["prefix"] == "datasets/qwen/"
    assert call_kwargs["delimiter"] == "/"
    assert call_kwargs["max_keys"] == 1000
    assert page.items == ["bench-1", "bench-2"]
    assert page.total == 2


def test_list_org_datasets_returns_empty_when_org_missing():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_org_datasets("nonexistent")

    assert page.items == []
    assert page.total == 0


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
        page = registry.list_dataset_splits("qwen", "bench")

    call_kwargs = mock_bucket.list_objects_v2.call_args[1]
    assert call_kwargs["prefix"] == "datasets/qwen/bench/"
    assert call_kwargs["delimiter"] == "/"
    assert page.items == ["test", "train"]
    assert page.total == 2


def test_list_dataset_splits_returns_empty_when_dataset_missing():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_dataset_splits("qwen", "nope")

    assert page.items == []
    assert page.total == 0


def test_list_all_datasets_returns_sorted_pairs():
    registry = OssDatasetRegistry(make_registry_info())

    def fake_list_org_datasets(org, *, offset=0, limit=None):
        data = {"qwen": ["bench-2", "bench-1"], "alibaba": ["pinch"]}
        return PageResult(items=data[org], total=len(data[org]), offset=0, limit=None)

    orgs_page = PageResult(items=["qwen", "alibaba"], total=2, offset=0, limit=None)
    with patch.object(registry, "list_organizations", return_value=orgs_page):
        with patch.object(registry, "list_org_datasets", side_effect=fake_list_org_datasets):
            page = registry.list_all_datasets()

    assert page.items == [("alibaba", "pinch"), ("qwen", "bench-1"), ("qwen", "bench-2")]
    assert page.total == 3


def test_list_all_datasets_uses_bounded_concurrency():
    registry = OssDatasetRegistry(make_registry_info())

    orgs_page = PageResult(items=["o1", "o2"], total=2, offset=0, limit=None)
    ds_page = PageResult(items=["d"], total=1, offset=0, limit=None)
    with patch.object(registry, "list_organizations", return_value=orgs_page):
        with patch.object(registry, "list_org_datasets", return_value=ds_page):
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

    orgs_page = PageResult(items=["o1"], total=1, offset=0, limit=None)
    ds_page = PageResult(items=["d"], total=1, offset=0, limit=None)
    with patch.object(registry, "list_organizations", return_value=orgs_page):
        with patch.object(registry, "list_org_datasets", return_value=ds_page):
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

    def fake_list_org_datasets(org, *, offset=0, limit=None):
        if org == "bad":
            raise RuntimeError("oss boom")
        return PageResult(items=["d"], total=1, offset=0, limit=None)

    orgs_page = PageResult(items=["good", "bad"], total=2, offset=0, limit=None)
    with patch.object(registry, "list_organizations", return_value=orgs_page):
        with patch.object(registry, "list_org_datasets", side_effect=fake_list_org_datasets):
            with _pytest.raises(RuntimeError, match="oss boom"):
                registry.list_all_datasets()


def test_list_all_datasets_empty_when_no_orgs():
    registry = OssDatasetRegistry(make_registry_info())
    empty_page = PageResult(items=[], total=0, offset=0, limit=None)
    with patch.object(registry, "list_organizations", return_value=empty_page):
        page = registry.list_all_datasets()

    assert page.items == []
    assert page.total == 0


def test_list_all_datasets_query_filters_pairs():
    registry = OssDatasetRegistry(make_registry_info())

    def fake_list_org_datasets(org, *, offset=0, limit=None):
        data = {"qwen": ["bench-2", "bench-1"], "alibaba": ["pinch"]}
        return PageResult(items=data[org], total=len(data[org]), offset=0, limit=None)

    orgs_page = PageResult(items=["qwen", "alibaba"], total=2, offset=0, limit=None)
    with patch.object(registry, "list_organizations", return_value=orgs_page):
        with patch.object(registry, "list_org_datasets", side_effect=fake_list_org_datasets):
            page = registry.list_all_datasets(query="pinch")

    assert page.items == [("alibaba", "pinch")]
    assert page.total == 1


def test_list_dataset_tasks_query_filters():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[
            "datasets/qwen/bench/test/dask__dask-001/",
            "datasets/qwen/bench/test/pydantic__pydantic-002/",
            "datasets/qwen/bench/test/dask__dask-003/",
        ]
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_dataset_tasks("qwen", "bench", "test", query="dask")

    assert page is not None
    assert page.items == ["dask__dask-001", "dask__dask-003"]
    assert page.total == 2


# ---------------------------------------------------------------------------
# list_dataset_task_entries tests
# ---------------------------------------------------------------------------


def test_list_dataset_task_entries_returns_dir_and_file_entries():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=["datasets/qwen/bench/test/task-dir/"],
        objects=[
            MagicMock(key="datasets/qwen/bench/test/task-file.json", size=2048, last_modified=1700000000.0, etag='"abc"'),
        ],
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_dataset_task_entries("qwen", "bench", "test")

    assert page is not None
    assert len(page.items) == 2

    d = page.items[0]
    assert d.name == "task-dir"
    assert d.type == "directory"
    assert d.size is None
    assert d.etag is None

    f = page.items[1]
    assert f.name == "task-file"
    assert f.path == "task-file.json"
    assert f.type == "file"
    assert f.size == 2048
    assert f.etag == '"abc"'
    assert f.file_count == 1
    assert f.updated_at is not None


def test_list_dataset_task_entries_ignores_placeholder_and_nested():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[],
        objects=[
            MagicMock(key="datasets/qwen/bench/test/", size=0, last_modified=0, etag=""),
            MagicMock(key="datasets/qwen/bench/test/nested/deep.json", size=100, last_modified=0, etag=""),
            MagicMock(key="datasets/qwen/bench/test/task-001.json", size=500, last_modified=1700000000.0, etag='"x"'),
        ],
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_dataset_task_entries("qwen", "bench", "test")

    assert page is not None
    assert len(page.items) == 1
    assert page.items[0].name == "task-001"


def test_list_dataset_task_entries_query_filters():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[
            "datasets/qwen/bench/test/dask__dask-001/",
            "datasets/qwen/bench/test/pydantic-002/",
        ]
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_dataset_task_entries("qwen", "bench", "test", query="dask")

    assert page is not None
    assert len(page.items) == 1
    assert page.items[0].name == "dask__dask-001"


def test_list_dataset_task_entries_returns_none_when_empty():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result()

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_dataset_task_entries("qwen", "bench", "test")

    assert page is None


def test_list_dataset_task_entries_pagination():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[
            "datasets/q/b/test/a/",
            "datasets/q/b/test/b/",
            "datasets/q/b/test/c/",
        ]
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_dataset_task_entries("q", "b", "test", offset=1, limit=1)

    assert page is not None
    assert page.total == 3
    assert len(page.items) == 1
    assert page.items[0].name == "b"


# ---------------------------------------------------------------------------
# browse_task_files tests
# ---------------------------------------------------------------------------


def test_browse_task_files_returns_dirs_and_files():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=["datasets/qwen/bench/test/task-1/data/"],
        objects=[
            MagicMock(
                key="datasets/qwen/bench/test/task-1/README.md",
                size=1234,
                last_modified=1700000000.0,
                etag='"md5"',
            ),
        ],
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.browse_task_files("qwen", "bench", "test", "task-1")

    assert len(page.items) == 2
    d = page.items[0]
    assert d.name == "data"
    assert d.path == "data"
    assert d.type == "directory"
    assert d.size is None

    f = page.items[1]
    assert f.name == "README.md"
    assert f.path == "README.md"
    assert f.type == "file"
    assert f.size == 1234
    assert f.media_type == "text/markdown"
    assert f.etag == '"md5"'
    assert f.updated_at is not None


def test_browse_task_files_with_prefix():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[],
        objects=[
            MagicMock(
                key="datasets/qwen/bench/test/task-1/data/input.json",
                size=500,
                last_modified=1700000000.0,
                etag='"e"',
            ),
        ],
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.browse_task_files("qwen", "bench", "test", "task-1", prefix="data")

    call_kwargs = mock_bucket.list_objects_v2.call_args[1]
    assert call_kwargs["prefix"] == "datasets/qwen/bench/test/task-1/data/"
    assert call_kwargs["delimiter"] == "/"
    assert len(page.items) == 1
    assert page.items[0].name == "input.json"
    assert page.items[0].path == "data/input.json"
    assert page.items[0].media_type == "application/json"


def test_browse_task_files_dirs_sorted_before_files():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=["datasets/q/b/t/task/zdir/"],
        objects=[
            MagicMock(key="datasets/q/b/t/task/afile.txt", size=10, last_modified=0, etag=""),
        ],
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.browse_task_files("q", "b", "t", "task")

    assert page.items[0].type == "directory"
    assert page.items[0].name == "zdir"
    assert page.items[1].type == "file"
    assert page.items[1].name == "afile.txt"


def test_browse_task_files_empty():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result()

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.browse_task_files("q", "b", "t", "task")

    assert page.items == []
    assert page.total == 0


def test_browse_task_files_ignores_placeholder():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        objects=[
            MagicMock(key="datasets/q/b/t/task/", size=0, last_modified=0, etag=""),
            MagicMock(key="datasets/q/b/t/task/real.txt", size=5, last_modified=0, etag=""),
        ],
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.browse_task_files("q", "b", "t", "task")

    assert len(page.items) == 1
    assert page.items[0].name == "real.txt"


# ---------------------------------------------------------------------------
# get_task_metadata tests
# ---------------------------------------------------------------------------


def test_get_task_metadata_finds_readme():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_get = MagicMock()
    mock_get.read.return_value = b"# Hello World"
    mock_bucket.get_object.return_value = mock_get

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        meta = registry.get_task_metadata("qwen", "bench", "test", "task-1")

    assert meta is not None
    assert meta.source == "README.md"
    assert meta.format == "markdown"
    assert meta.content == "# Hello World"
    assert meta.parsed is None
    assert meta.generated is False


def test_get_task_metadata_fallback_to_metadata_json():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()

    def fake_get_object(key):
        if key.endswith("README.md") or key.endswith("readme.md"):
            raise oss2.exceptions.NoSuchKey(404, {}, b"", {})
        result = MagicMock()
        result.read.return_value = b'{"title": "Task 1"}'
        return result

    mock_bucket.get_object.side_effect = fake_get_object

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        meta = registry.get_task_metadata("qwen", "bench", "test", "task-1")

    assert meta is not None
    assert meta.source == "metadata.json"
    assert meta.format == "json"
    assert meta.parsed == {"title": "Task 1"}
    assert meta.generated is False


def test_get_task_metadata_generated_fallback():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {})

    mock_list_result = make_list_result(
        objects=[
            MagicMock(key="datasets/qwen/bench/test/task-1/data.json", size=100, last_modified=1700000000.0, etag=""),
        ],
    )
    mock_bucket.list_objects_v2.return_value = mock_list_result

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        meta = registry.get_task_metadata("qwen", "bench", "test", "task-1")

    assert meta is not None
    assert meta.source == "generated"
    assert meta.format == "markdown"
    assert meta.generated is True
    assert "data.json" in meta.content
    assert "100 bytes" in meta.content


def test_get_task_metadata_returns_none_when_no_files():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {})
    mock_bucket.list_objects_v2.return_value = make_list_result()

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        meta = registry.get_task_metadata("qwen", "bench", "test", "task-1")

    assert meta is None
