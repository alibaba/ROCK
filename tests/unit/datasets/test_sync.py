from dataclasses import dataclass
from unittest.mock import MagicMock

from rock.sdk.envhub.datasets.sync import (
    DatasetObject,
    DatasetOssStore,
    DatasetSyncService,
)


@dataclass
class FakeObject:
    key: str
    size: int = 100
    etag: str = "abc"
    last_modified: int = 1000

    def is_prefix(self):
        return False


def make_list_result(objects=None, is_truncated=False):
    result = MagicMock()
    result.object_list = objects or []
    result.prefix_list = []
    result.is_truncated = is_truncated
    result.next_continuation_token = ""
    return result


def test_store_list_objects_recursive():
    bucket = MagicMock()
    bucket.list_objects_v2.return_value = make_list_result(
        objects=[FakeObject(key="datasets/qwen/bench/a.txt"), FakeObject(key="datasets/qwen/bench/b.txt")]
    )
    store = DatasetOssStore(bucket)
    result = store.list_objects_recursive("qwen/bench")
    assert set(result.keys()) == {"a.txt", "b.txt"}


def test_store_copy_from_uses_copy_object():
    source_bucket = MagicMock()
    source_bucket.bucket_name = "source-bucket"
    target_bucket = MagicMock()

    source_store = DatasetOssStore(source_bucket)
    target_store = DatasetOssStore(target_bucket)

    obj = DatasetObject(key="datasets/qwen/bench/a.txt", relative_path="a.txt", size=100, etag="abc")
    target_store.copy_from(source_store, obj, "qwen/bench", "a.txt")

    target_bucket.copy_object.assert_called_once_with("source-bucket", "datasets/qwen/bench/a.txt", "datasets/qwen/bench/a.txt")


def test_store_copy_from_fallback_to_get_put():
    source_bucket = MagicMock()
    source_bucket.bucket_name = "source-bucket"
    target_bucket = MagicMock()
    target_bucket.copy_object.side_effect = Exception("cross-region")

    source_store = DatasetOssStore(source_bucket)
    target_store = DatasetOssStore(target_bucket)

    obj = DatasetObject(key="datasets/qwen/bench/a.txt", relative_path="a.txt", size=100, etag="abc")
    target_store.copy_from(source_store, obj, "qwen/bench", "a.txt")

    source_bucket.get_object.assert_called_once_with("datasets/qwen/bench/a.txt")
    target_bucket.put_object.assert_called_once()


def test_sync_dry_run_returns_diff():
    source_bucket = MagicMock()
    target_bucket = MagicMock()

    source_bucket.list_objects_v2.return_value = make_list_result(
        objects=[FakeObject(key="datasets/qwen/bench/a.txt", size=100, etag="abc")]
    )
    target_bucket.list_objects_v2.return_value = make_list_result(objects=[])

    service = DatasetSyncService(DatasetOssStore(source_bucket), DatasetOssStore(target_bucket))
    result = service.sync("qwen/bench", dry_run=True)

    assert result.dry_run is True
    assert result.summary.to_copy == 1
    assert result.summary.copied == 0
    assert result.diff is not None
    assert result.diff.to_copy.total == 1


def test_sync_execute_copies_objects():
    source_bucket = MagicMock()
    target_bucket = MagicMock()

    source_bucket.list_objects_v2.return_value = make_list_result(
        objects=[FakeObject(key="datasets/qwen/bench/a.txt", size=100, etag="abc")]
    )
    target_bucket.list_objects_v2.return_value = make_list_result(objects=[])

    service = DatasetSyncService(DatasetOssStore(source_bucket), DatasetOssStore(target_bucket))
    result = service.sync("qwen/bench", dry_run=False)

    assert result.dry_run is False
    assert result.summary.copied == 1
    assert result.diff is None


def test_sync_skips_identical_objects():
    source_bucket = MagicMock()
    target_bucket = MagicMock()

    obj = FakeObject(key="datasets/qwen/bench/a.txt", size=100, etag="abc")
    source_bucket.list_objects_v2.return_value = make_list_result(objects=[obj])
    target_bucket.list_objects_v2.return_value = make_list_result(objects=[obj])

    service = DatasetSyncService(DatasetOssStore(source_bucket), DatasetOssStore(target_bucket))
    result = service.sync("qwen/bench", dry_run=True)

    assert result.summary.to_copy == 0
    assert result.summary.skipped == 1


def test_sync_delete_extra():
    source_bucket = MagicMock()
    target_bucket = MagicMock()

    source_bucket.list_objects_v2.return_value = make_list_result(objects=[])
    target_bucket.list_objects_v2.return_value = make_list_result(
        objects=[FakeObject(key="datasets/qwen/bench/extra.txt")]
    )

    service = DatasetSyncService(DatasetOssStore(source_bucket), DatasetOssStore(target_bucket))
    result = service.sync("qwen/bench", dry_run=False, delete_extra=True)

    assert result.summary.deleted == 1
    target_bucket.delete_object.assert_called_once()
