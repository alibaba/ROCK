"""Dataset sync: incremental copy between OSS buckets.

Adapted from harbor viewer ``DatasetSyncService``.  Only the synchronous
``sync()`` path is kept — async job management is not included.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import oss2
from pydantic import BaseModel, Field

from rock.logger import init_logger

logger = init_logger(__name__)

_LIST_MAX_KEYS = 1000
_DATASETS_PREFIX = "datasets"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DatasetObject(BaseModel):
    key: str
    relative_path: str
    size: int
    etag: str | None = None
    last_modified: int | None = None


class DatasetSyncSummary(BaseModel):
    source_objects: int = 0
    target_objects: int = 0
    to_copy: int = 0
    to_delete: int = 0
    copied: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0


class DatasetSyncFailure(BaseModel):
    path: str
    operation: str
    message: str


class DatasetSyncDiffList(BaseModel):
    items: list[str] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False
    omitted: int = 0


class DatasetSyncDiff(BaseModel):
    limit: int = 100
    to_copy: DatasetSyncDiffList
    to_delete: DatasetSyncDiffList


class DatasetSyncResult(BaseModel):
    dataset: str
    path: str = ""
    scope: str
    dry_run: bool
    delete_extra: bool
    summary: DatasetSyncSummary
    diff: DatasetSyncDiff | None = None
    failures: list[DatasetSyncFailure] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass
class _PrefixInfo:
    key: str

    def is_prefix(self) -> bool:
        return True


def _iter_bucket_objects(bucket: Any, prefix: str, *, delimiter: str | None = None) -> Iterator[Any]:
    continuation_token = ""
    delimiter_value = delimiter or ""
    while True:
        result = bucket.list_objects_v2(
            prefix=prefix,
            delimiter=delimiter_value,
            continuation_token=continuation_token,
            max_keys=_LIST_MAX_KEYS,
        )
        entries = list(getattr(result, "object_list", []) or [])
        for p in getattr(result, "prefix_list", []) or []:
            entries.append(_PrefixInfo(key=p if isinstance(p, str) else getattr(p, "key", str(p))))
        entries.sort(key=lambda obj: obj.key)
        yield from entries

        if not getattr(result, "is_truncated", False):
            break
        continuation_token = getattr(result, "next_continuation_token", "")
        if not continuation_token:
            raise RuntimeError("OSS listing truncated without continuation token")


def _not_found_exceptions() -> tuple[type[BaseException], ...]:
    exceptions: list[type[BaseException]] = [KeyError]
    for name in ("NoSuchKey", "NotFound"):
        exc = getattr(oss2.exceptions, name, None)
        if isinstance(exc, type) and issubclass(exc, BaseException):
            exceptions.append(exc)
    return tuple(exceptions)


# ---------------------------------------------------------------------------
# DatasetOssStore
# ---------------------------------------------------------------------------


class DatasetOssStore:
    """OSS operations scoped to the ``datasets/`` prefix of a single bucket."""

    def __init__(self, bucket: oss2.Bucket) -> None:
        self._bucket = bucket

    @property
    def bucket(self) -> oss2.Bucket:
        return self._bucket

    @property
    def bucket_name(self) -> str:
        return getattr(self._bucket, "bucket_name", "")

    def _key(self, dataset: str, path: str | None = None) -> str:
        key = f"{_DATASETS_PREFIX}/{dataset}"
        if path:
            key = f"{key}/{path}"
        return key

    def _prefix(self, dataset: str, path: str | None = None) -> str:
        key = self._key(dataset, path)
        return key if key.endswith("/") else f"{key}/"

    def get_object_metadata(self, dataset: str, path: str) -> DatasetObject | None:
        if not path:
            return None
        key = self._key(dataset, path)
        try:
            head = self._bucket.head_object(key)
        except _not_found_exceptions():
            return None
        return DatasetObject(
            key=key,
            relative_path=path,
            size=getattr(head, "content_length", getattr(head, "size", 0)),
            etag=getattr(head, "etag", None),
            last_modified=getattr(head, "last_modified", None),
        )

    def is_dir(self, dataset: str, path: str) -> bool:
        prefix = self._prefix(dataset, path)
        for _ in _iter_bucket_objects(self._bucket, prefix):
            return True
        return False

    def list_objects_recursive(self, dataset: str, prefix: str = "") -> dict[str, DatasetObject]:
        base_prefix = self._prefix(dataset, prefix)
        result: dict[str, DatasetObject] = {}
        for obj in _iter_bucket_objects(self._bucket, base_prefix):
            relative = obj.key[len(base_prefix):]
            if not relative or obj.key.endswith("/"):
                continue
            result[relative] = DatasetObject(
                key=obj.key,
                relative_path=relative,
                size=getattr(obj, "size", 0),
                etag=getattr(obj, "etag", None),
                last_modified=getattr(obj, "last_modified", None),
            )
        return result

    def copy_from(self, source: DatasetOssStore, source_object: DatasetObject, dataset: str, path: str) -> None:
        target_key = self._key(dataset, path)
        try:
            self._bucket.copy_object(source.bucket_name, source_object.key, target_key)
            return
        except Exception:
            pass
        stream = source._bucket.get_object(source_object.key)
        self._bucket.put_object(target_key, stream)

    def delete_file(self, dataset: str, path: str) -> None:
        self._bucket.delete_object(self._key(dataset, path))


# ---------------------------------------------------------------------------
# DatasetSyncService
# ---------------------------------------------------------------------------


@dataclass
class _SyncPlan:
    scope: str
    path_prefix: str
    source_objects: dict[str, DatasetObject]
    target_objects: dict[str, DatasetObject]
    to_copy: list[str]
    to_delete: list[str]
    skipped: int


def _build_diff_list(paths: list[str], prefix: str, limit: int) -> DatasetSyncDiffList:
    items = [f"{prefix}/{p}" if prefix else p for p in paths]
    preview = items[:limit]
    omitted = len(items) - len(preview)
    return DatasetSyncDiffList(items=preview, total=len(items), truncated=omitted > 0, omitted=omitted)


class DatasetSyncService:
    """Incrementally sync dataset objects from one OSS store to another."""

    def __init__(self, source: DatasetOssStore, target: DatasetOssStore) -> None:
        self._source = source
        self._target = target

    def _resolve_scope(self, dataset: str, path: str, requested_scope: str) -> str:
        if not path:
            if requested_scope == "file":
                raise ValueError("Source file not found")
            return "folder"
        if requested_scope == "file":
            if self._source.get_object_metadata(dataset, path) is None:
                raise ValueError("Source file not found")
            return "file"
        if requested_scope == "folder":
            if not self._source.is_dir(dataset, path):
                raise ValueError("Source folder not found")
            return "folder"
        if self._source.get_object_metadata(dataset, path) is not None:
            return "file"
        if self._source.is_dir(dataset, path):
            return "folder"
        raise ValueError("Source path not found")

    def _plan(self, dataset: str, path: str, scope: str, delete_extra: bool) -> _SyncPlan:
        if scope == "file":
            source_object = self._source.get_object_metadata(dataset, path)
            if source_object is None:
                raise ValueError("Source file not found")
            target_object = self._target.get_object_metadata(dataset, path)
            source_objects = {path: source_object}
            target_objects = {path: target_object} if target_object else {}
            path_prefix = ""
        else:
            source_objects = self._source.list_objects_recursive(dataset, path)
            target_objects = self._target.list_objects_recursive(dataset, path)
            path_prefix = path

        to_copy: list[str] = []
        for relative, source_obj in source_objects.items():
            target_obj = target_objects.get(relative)
            if target_obj is None or source_obj.size != target_obj.size or source_obj.etag != target_obj.etag:
                to_copy.append(relative)

        to_delete = sorted(set(target_objects) - set(source_objects)) if delete_extra and scope == "folder" else []
        to_copy.sort()
        skipped = len(source_objects) - len(to_copy)
        return _SyncPlan(
            scope=scope,
            path_prefix=path_prefix,
            source_objects=source_objects,
            target_objects=target_objects,
            to_copy=to_copy,
            to_delete=to_delete,
            skipped=skipped,
        )

    def sync(
        self,
        dataset: str,
        path: str = "",
        *,
        scope: str = "auto",
        dry_run: bool = True,
        delete_extra: bool = False,
        diff_limit: int = 100,
    ) -> DatasetSyncResult:
        resolved_scope = self._resolve_scope(dataset, path, scope)
        plan = self._plan(dataset, path, resolved_scope, delete_extra)

        summary = DatasetSyncSummary(
            source_objects=len(plan.source_objects),
            target_objects=len(plan.target_objects),
            to_copy=len(plan.to_copy),
            to_delete=len(plan.to_delete),
            skipped=plan.skipped,
        )

        diff = (
            DatasetSyncDiff(
                limit=diff_limit,
                to_copy=_build_diff_list(plan.to_copy, plan.path_prefix, diff_limit),
                to_delete=_build_diff_list(plan.to_delete, plan.path_prefix, diff_limit),
            )
            if dry_run
            else None
        )

        failures: list[DatasetSyncFailure] = []

        if not dry_run:
            for relative in plan.to_copy:
                target_path = f"{plan.path_prefix}/{relative}" if plan.path_prefix else relative
                try:
                    self._target.copy_from(self._source, plan.source_objects[relative], dataset, target_path)
                    summary.copied += 1
                except Exception as exc:
                    failures.append(DatasetSyncFailure(path=target_path, operation="copy", message=str(exc)))

            for relative in plan.to_delete:
                target_path = f"{plan.path_prefix}/{relative}" if plan.path_prefix else relative
                try:
                    self._target.delete_file(dataset, target_path)
                    summary.deleted += 1
                except Exception as exc:
                    failures.append(DatasetSyncFailure(path=target_path, operation="delete", message=str(exc)))

            summary.failed = len(failures)

        return DatasetSyncResult(
            dataset=dataset,
            path=path,
            scope=resolved_scope,
            dry_run=dry_run,
            delete_extra=delete_extra,
            summary=summary,
            diff=diff,
            failures=failures,
        )
