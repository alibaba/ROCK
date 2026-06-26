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
    ]

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_datasets()

    assert len(page.items) == 1
    assert page.items[0].id == "qwen/my-bench"
    assert page.items[0].split == "train"
    assert page.items[0].task_ids == []


def test_list_datasets_filter_by_org():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.side_effect = [
        make_list_result(prefixes=["datasets/qwen/my-bench/"]),
        make_list_result(prefixes=["datasets/qwen/my-bench/train/"]),
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
    ]

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        page = registry.list_datasets()

    assert len(page.items) == 1
    assert page.items[0].id == "qwen/my-bench"
    assert page.items[0].split == "train"
    assert page.items[0].task_ids == []


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
            "datasets/qwen/my-bench/test/task-001/",
            "datasets/qwen/my-bench/test/task-002/",
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
    assert mock_bucket.put_object.call_count == 1


def test_upload_dataset_oss_key_format(tmp_path):
    (tmp_path / "task-001").mkdir()
    (tmp_path / "task-001" / "task.toml").write_text("[task]")

    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(objects=[])
    source, target = make_upload_pair(tmp_path)

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        registry.upload_dataset(source, target)

    assert mock_bucket.put_object.call_args[0][0] == "datasets/qwen/my-bench/train/task-001/task.toml"


# ---------------------------------------------------------------------------
# list_organizations tests
# ---------------------------------------------------------------------------


def test_list_organizations_returns_sorted_org_names():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[
            "datasets/AoneBenchDev/",
            "datasets/alibaba/",
            "datasets/qwen/",
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
            "datasets/qwen/bench-1/",
            "datasets/qwen/bench-2/",
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
            "datasets/qwen/bench/test/",
            "datasets/qwen/bench/train/",
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

    def fake_list_org_dataset_names(org):
        data = {"qwen": ["bench-2", "bench-1"], "alibaba": ["pinch"]}
        return data[org]

    orgs_page = PageResult(items=["qwen", "alibaba"], total=2, offset=0, limit=None)
    with patch.object(registry, "list_organizations", return_value=orgs_page):
        with patch.object(registry, "_list_org_dataset_names", side_effect=fake_list_org_dataset_names):
            page = registry.list_all_datasets()

    assert page.items == [("alibaba", "pinch"), ("qwen", "bench-1"), ("qwen", "bench-2")]
    assert page.total == 3


def test_list_all_datasets_uses_bounded_concurrency():
    registry = OssDatasetRegistry(make_registry_info())

    orgs_page = PageResult(items=["o1", "o2"], total=2, offset=0, limit=None)
    with patch.object(registry, "list_organizations", return_value=orgs_page):
        with patch.object(registry, "_list_org_dataset_names", return_value=["d"]):
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
    with patch.object(registry, "list_organizations", return_value=orgs_page):
        with patch.object(registry, "_list_org_dataset_names", return_value=["d"]):
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

    def fake_list_org_dataset_names(org):
        if org == "bad":
            raise RuntimeError("oss boom")
        return ["d"]

    orgs_page = PageResult(items=["good", "bad"], total=2, offset=0, limit=None)
    with patch.object(registry, "list_organizations", return_value=orgs_page):
        with patch.object(registry, "_list_org_dataset_names", side_effect=fake_list_org_dataset_names):
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

    def fake_list_org_dataset_names(org):
        data = {"qwen": ["bench-2", "bench-1"], "alibaba": ["pinch"]}
        return data[org]

    orgs_page = PageResult(items=["qwen", "alibaba"], total=2, offset=0, limit=None)
    with patch.object(registry, "list_organizations", return_value=orgs_page):
        with patch.object(registry, "_list_org_dataset_names", side_effect=fake_list_org_dataset_names):
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
            MagicMock(
                key="datasets/qwen/bench/test/task-file.json", size=2048, last_modified=1700000000.0, etag='"abc"'
            ),
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


# ---------------------------------------------------------------------------
# _count_dir_entries / _list_dir_names tests
# ---------------------------------------------------------------------------


def test_count_dir_entries_counts_prefixes_only():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[
            "datasets/qwen/bench/test/task-001/",
            "datasets/qwen/bench/test/task-002/",
            "datasets/qwen/bench/test/task-003/",
        ],
        objects=[
            MagicMock(key="datasets/qwen/bench/test/task-file.json"),
        ],
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        count = registry._count_dir_entries("datasets/qwen/bench/test/")

    assert count == 3


def test_count_dir_entries_paginates_sequential():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    page1 = make_list_result(
        prefixes=["datasets/q/b/t/a/", "datasets/q/b/t/b/"],
        is_truncated=True,
    )
    page1.next_continuation_token = "tok1"
    page2 = make_list_result(
        prefixes=["datasets/q/b/t/c/"],
    )
    mock_bucket.list_objects_v2.side_effect = [page1, page2]

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        count = registry._count_dir_entries("datasets/q/b/t/", concurrency=1)

    assert count == 3
    assert mock_bucket.list_objects_v2.call_count == 2


def test_count_dir_entries_parallel():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()

    first_page = make_list_result(
        prefixes=["datasets/q/b/t/a/", "datasets/q/b/t/b/"],
        is_truncated=True,
    )
    first_page.next_continuation_token = "tok1"

    partition_page = make_list_result(prefixes=["datasets/q/b/t/c/"])

    mock_bucket.list_objects_v2.side_effect = [first_page] + [partition_page] * 3

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        count = registry._count_dir_entries("datasets/q/b/t/", concurrency=4)

    assert count == 2 + 3


def test_split_key_range_generates_partition_points():
    from rock.sdk.envhub.datasets.registry.oss import _split_key_range

    points = _split_key_range("a", 3)
    assert len(points) == 3
    assert all(isinstance(p, str) and len(p) == 1 for p in points)


def test_split_key_range_single_partition():
    from rock.sdk.envhub.datasets.registry.oss import _split_key_range

    points = _split_key_range("a", 1)
    assert points == ["a"]


def test_split_key_range_high_start():
    from rock.sdk.envhub.datasets.registry.oss import _split_key_range

    points = _split_key_range("~", 3)
    assert points == ["~"] * 3


def test_list_dir_names_returns_names():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=[
            "datasets/qwen/bench/test/alpha/",
            "datasets/qwen/bench/test/beta/",
        ]
    )

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        names = registry._list_dir_names("datasets/qwen/bench/test/")

    assert names == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# get_dataset tests (uses _count_dir_entries)
# ---------------------------------------------------------------------------


def test_get_dataset_returns_info_with_task_counts():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.side_effect = [
        make_list_result(prefixes=["datasets/qwen/bench/test/", "datasets/qwen/bench/train/"]),
        make_list_result(prefixes=["datasets/qwen/bench/test/t1/", "datasets/qwen/bench/test/t2/"]),
        make_list_result(prefixes=["datasets/qwen/bench/train/t1/", "datasets/qwen/bench/train/t2/", "datasets/qwen/bench/train/t3/"]),
    ]

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        info = registry.get_dataset("qwen", "bench")

    assert info is not None
    assert info.id == "qwen/bench"
    assert info.splits == ["test", "train"]
    assert info.task_counts == {"test": 2, "train": 3}


def test_get_dataset_returns_none_when_no_splits():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(prefixes=[])

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        info = registry.get_dataset("qwen", "nonexistent")

    assert info is None


# ---------------------------------------------------------------------------
# meta management tests
# ---------------------------------------------------------------------------


def test_read_meta_returns_parsed_json():
    import json

    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    meta = {"splits": {"test": {"task_count": 42}}}
    mock_result = MagicMock()
    mock_result.read.return_value = json.dumps(meta).encode()
    mock_bucket.get_object.return_value = mock_result

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        result = registry._read_meta("qwen", "bench")

    assert result == meta
    mock_bucket.get_object.assert_called_once_with("meta/qwen/bench/meta.json")


def test_read_meta_returns_none_on_no_such_key():
    import oss2.exceptions

    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.get_object.side_effect = oss2.exceptions.NoSuchKey(404, {}, b"", {})

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        result = registry._read_meta("qwen", "bench")

    assert result is None


def test_write_meta_puts_json():
    import json

    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    meta = {"splits": {"test": {"task_count": 10}}}

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        registry._write_meta("qwen", "bench", meta)

    mock_bucket.put_object.assert_called_once()
    key, data = mock_bucket.put_object.call_args[0]
    assert key == "meta/qwen/bench/meta.json"
    assert json.loads(data) == meta


def test_get_dataset_uses_meta_cache():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(
        prefixes=["datasets/qwen/bench/test/", "datasets/qwen/bench/train/"]
    )
    meta = {"splits": {"test": {"task_count": 100}, "train": {"task_count": 200}}}

    with (
        patch.object(registry, "_build_bucket", return_value=mock_bucket),
        patch.object(registry, "_read_meta", return_value=meta),
    ):
        info = registry.get_dataset("qwen", "bench")

    assert info is not None
    assert info.task_counts == {"test": 100, "train": 200}
    assert mock_bucket.list_objects_v2.call_count == 1


def test_get_dataset_falls_back_when_meta_missing():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.side_effect = [
        make_list_result(prefixes=["datasets/qwen/bench/test/"]),
        make_list_result(prefixes=["datasets/qwen/bench/test/t1/", "datasets/qwen/bench/test/t2/"]),
    ]

    with (
        patch.object(registry, "_build_bucket", return_value=mock_bucket),
        patch.object(registry, "_read_meta", return_value=None),
    ):
        info = registry.get_dataset("qwen", "bench")

    assert info is not None
    assert info.task_counts == {"test": 2}


def test_get_dataset_falls_back_when_meta_incomplete():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.side_effect = [
        make_list_result(prefixes=["datasets/qwen/bench/test/", "datasets/qwen/bench/train/"]),
        make_list_result(prefixes=["datasets/qwen/bench/train/t1/"]),
    ]
    meta = {"splits": {"test": {"task_count": 50}}}

    with (
        patch.object(registry, "_build_bucket", return_value=mock_bucket),
        patch.object(registry, "_read_meta", return_value=meta),
    ):
        info = registry.get_dataset("qwen", "bench")

    assert info is not None
    assert info.task_counts == {"test": 50, "train": 1}


def test_upload_dataset_does_not_update_meta(tmp_path):
    (tmp_path / "task-001").mkdir()
    (tmp_path / "task-001" / "data.json").write_text("{}")

    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.return_value = make_list_result(objects=[])
    source, target = make_upload_pair(tmp_path)

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        registry.upload_dataset(source, target)

    meta_calls = [c for c in mock_bucket.put_object.call_args_list if c[0][0].startswith("meta/")]
    assert len(meta_calls) == 0


def test_refresh_metadata_counts_all_splits():
    registry = OssDatasetRegistry(make_registry_info())
    mock_bucket = MagicMock()
    mock_bucket.list_objects_v2.side_effect = [
        make_list_result(prefixes=["datasets/qwen/bench/test/", "datasets/qwen/bench/train/"]),
        make_list_result(prefixes=["datasets/qwen/bench/test/t1/", "datasets/qwen/bench/test/t2/"]),
        make_list_result(prefixes=["datasets/qwen/bench/train/t1/"]),
    ]

    with patch.object(registry, "_build_bucket", return_value=mock_bucket):
        meta = registry.refresh_metadata("qwen", "bench")

    assert meta == {"splits": {"test": {"task_count": 2}, "train": {"task_count": 1}}}
    meta_calls = [c for c in mock_bucket.put_object.call_args_list if c[0][0].startswith("meta/")]
    assert len(meta_calls) == 1


# ---------------------------------------------------------------------------
# sync_dataset tests
# ---------------------------------------------------------------------------


def test_sync_dataset_dry_run():
    from rock.sdk.envhub.datasets.sync import DatasetSyncResult, DatasetSyncSummary

    registry = OssDatasetRegistry(make_registry_info())
    target_info = make_registry_info()
    expected = DatasetSyncResult(
        dataset="qwen/bench",
        scope="folder",
        dry_run=True,
        delete_extra=False,
        summary=DatasetSyncSummary(source_objects=1, to_copy=1),
    )

    with patch.object(registry, "_build_bucket", return_value=MagicMock()):
        with patch("rock.sdk.envhub.datasets.registry.oss.DatasetSyncService") as MockService:
            MockService.return_value.sync.return_value = expected
            result = registry.sync_dataset("qwen/bench", target_info, dry_run=True)

    assert result.dry_run is True
    assert result.summary.to_copy == 1


def test_sync_dataset_execute_does_not_refresh_meta():
    from rock.sdk.envhub.datasets.sync import DatasetSyncResult, DatasetSyncSummary

    registry = OssDatasetRegistry(make_registry_info())
    target_info = make_registry_info()
    expected = DatasetSyncResult(
        dataset="qwen/bench",
        scope="folder",
        dry_run=False,
        delete_extra=False,
        summary=DatasetSyncSummary(source_objects=2, copied=2),
    )

    with patch.object(registry, "_build_bucket", return_value=MagicMock()):
        with patch("rock.sdk.envhub.datasets.registry.oss.DatasetSyncService") as MockService:
            MockService.return_value.sync.return_value = expected
            with patch.object(registry, "refresh_metadata") as mock_refresh:
                result = registry.sync_dataset("qwen/bench", target_info, dry_run=False)

    assert result.summary.copied == 2
    mock_refresh.assert_not_called()
