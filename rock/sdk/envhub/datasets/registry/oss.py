from __future__ import annotations

import json as _json
import mimetypes
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import oss2

from rock.logger import init_logger
from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
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
from rock.sdk.envhub.datasets.registry.base import BaseDatasetRegistry

logger = init_logger(__name__)

_LIST_MAX_KEYS = 1000


def _paginate(items: list, offset: int = 0, limit: int | None = None) -> PageResult:
    total = len(items)
    end = offset + limit if limit is not None else None
    return PageResult(items=items[offset:end], total=total, offset=offset, limit=limit)


_METADATA_CANDIDATES = [
    ("README.md", "markdown"),
    ("readme.md", "markdown"),
    ("metadata.json", "json"),
    ("task.toml", "toml"),
]


@dataclass
class _PrefixEntry:
    key: str

    def is_prefix(self) -> bool:
        return True


class OssDatasetRegistry(BaseDatasetRegistry):
    def __init__(self, registry: OssRegistryInfo) -> None:
        self._registry = registry
        self._cached_bucket: oss2.Bucket | None = None

    def _build_bucket(self) -> oss2.Bucket:
        if self._cached_bucket is None:
            auth = oss2.Auth(
                self._registry.oss_access_key_id or "",
                self._registry.oss_access_key_secret or "",
            )
            self._cached_bucket = oss2.Bucket(auth, self._registry.oss_endpoint or "", self._registry.oss_bucket)
        return self._cached_bucket

    def _build_prefix(self, org: str, name: str, split: str | None = None) -> str:
        base = self._registry.oss_dataset_path or "datasets"
        parts = [base, org, name]
        if split:
            parts.append(split)
        return "/".join(parts)

    @staticmethod
    def _last_segment(prefix: str) -> str:
        return prefix.rstrip("/").rsplit("/", 1)[-1]

    def _iter_objects(self, prefix: str, *, delimiter: str | None = None) -> Iterator[Any]:
        bucket = self._build_bucket()
        marker = ""
        while True:
            kwargs: dict[str, Any] = dict(
                prefix=prefix, continuation_token=marker, max_keys=_LIST_MAX_KEYS
            )
            if delimiter is not None:
                kwargs["delimiter"] = delimiter
            result = bucket.list_objects_v2(**kwargs)

            if delimiter is not None:
                for p in result.prefix_list:
                    yield _PrefixEntry(key=p)
            yield from result.object_list

            if result.is_truncated:
                marker = result.next_continuation_token
            else:
                break

    def _list_prefixes(self, prefix: str) -> list[str]:
        return [
            e.key
            for e in self._iter_objects(prefix, delimiter="/")
            if isinstance(e, _PrefixEntry)
        ]

    def _extract_tasks_from_split(self, split_prefix: str) -> list[str]:
        dir_tasks: list[str] = []
        file_tasks: list[str] = []

        for entry in self._iter_objects(split_prefix, delimiter="/"):
            if isinstance(entry, _PrefixEntry):
                dir_tasks.append(self._last_segment(entry.key))
            else:
                key = entry.key
                if key.endswith("/"):
                    continue
                relative = key[len(split_prefix) :]
                if "/" in relative:
                    continue
                name = relative.rsplit(".", 1)[0] if "." in relative else relative
                file_tasks.append(name)

        return sorted(set(dir_tasks + file_tasks))

    def list_organizations(self, *, offset: int = 0, limit: int | None = None) -> PageResult[str]:
        base = self._registry.oss_dataset_path or "datasets"
        orgs = sorted(self._last_segment(p) for p in self._list_prefixes(f"{base}/"))
        return _paginate(orgs, offset, limit)

    def list_org_datasets(
        self, organization: str, *, offset: int = 0, limit: int | None = None
    ) -> PageResult[str]:
        base = self._registry.oss_dataset_path or "datasets"
        datasets = sorted(self._last_segment(p) for p in self._list_prefixes(f"{base}/{organization}/"))
        return _paginate(datasets, offset, limit)

    def list_dataset_splits(
        self, organization: str, dataset: str, *, offset: int = 0, limit: int | None = None
    ) -> PageResult[str]:
        base = self._registry.oss_dataset_path or "datasets"
        splits = sorted(self._last_segment(p) for p in self._list_prefixes(f"{base}/{organization}/{dataset}/"))
        return _paginate(splits, offset, limit)

    def list_all_datasets(
        self, concurrency: int = 10, *, query: str | None = None, offset: int = 0, limit: int | None = None
    ) -> PageResult[tuple[str, str]]:
        orgs = self.list_organizations().items
        if not orgs:
            return PageResult(items=[], total=0, offset=offset, limit=limit)
        pairs: list[tuple[str, str]] = []
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            future_to_org = {ex.submit(lambda o: self.list_org_datasets(o).items, o): o for o in orgs}
            for fut in as_completed(future_to_org):
                org = future_to_org[fut]
                for ds in fut.result():
                    pairs.append((org, ds))
        pairs.sort()
        if query:
            q_lower = query.lower()
            pairs = [p for p in pairs if q_lower in f"{p[0]}/{p[1]}".lower()]
        return _paginate(pairs, offset, limit)

    def list_datasets(
        self, organization: str | None = None, *, offset: int = 0, limit: int | None = None
    ) -> PageResult[DatasetSpec]:
        base = self._registry.oss_dataset_path or "datasets"

        if organization:
            org_prefixes = [f"{base}/{organization}/"]
        else:
            org_prefixes = self._list_prefixes(f"{base}/")

        def _scan_org(org_prefix: str) -> list[DatasetSpec]:
            org = self._last_segment(org_prefix)
            specs: list[DatasetSpec] = []
            for ds_prefix in self._list_prefixes(org_prefix):
                name = self._last_segment(ds_prefix)
                for split_prefix in self._list_prefixes(ds_prefix):
                    split = self._last_segment(split_prefix)
                    task_ids = self._extract_tasks_from_split(split_prefix)
                    specs.append(DatasetSpec(id=f"{org}/{name}", split=split, task_ids=task_ids))
            return specs

        datasets: list[DatasetSpec] = []
        if len(org_prefixes) > 1:
            with ThreadPoolExecutor(max_workers=min(len(org_prefixes), 10)) as ex:
                for specs in ex.map(_scan_org, org_prefixes):
                    datasets.extend(specs)
        else:
            for p in org_prefixes:
                datasets.extend(_scan_org(p))

        return _paginate(datasets, offset, limit)

    def list_dataset_tasks(
        self,
        organization: str,
        dataset: str,
        split: str = "test",
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[str] | None:
        split_prefix = f"{self._build_prefix(organization, dataset, split)}/"
        task_ids = self._extract_tasks_from_split(split_prefix)

        if not task_ids:
            return None

        if query:
            q_lower = query.lower()
            task_ids = [t for t in task_ids if q_lower in t.lower()]

        return _paginate(task_ids, offset, limit)

    def _extract_task_entries_from_split(self, split_prefix: str) -> list[TaskEntry]:
        dir_entries: list[TaskEntry] = []
        file_entries: list[TaskEntry] = []

        for entry in self._iter_objects(split_prefix, delimiter="/"):
            if isinstance(entry, _PrefixEntry):
                name = self._last_segment(entry.key)
                dir_entries.append(TaskEntry(name=name, path=name, type="directory"))
            else:
                key = entry.key
                if key.endswith("/"):
                    continue
                relative = key[len(split_prefix) :]
                if "/" in relative:
                    continue
                name = relative.rsplit(".", 1)[0] if "." in relative else relative
                updated_at = datetime.fromtimestamp(entry.last_modified, tz=timezone.utc).isoformat()
                file_entries.append(
                    TaskEntry(
                        name=name,
                        path=relative,
                        type="file",
                        size=entry.size,
                        file_count=1,
                        updated_at=updated_at,
                        etag=entry.etag,
                    )
                )

        all_entries = dir_entries + file_entries
        all_entries.sort(key=lambda e: e.name)
        return all_entries

    def list_dataset_task_entries(
        self,
        organization: str,
        dataset: str,
        split: str = "test",
        *,
        query: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[TaskEntry] | None:
        split_prefix = f"{self._build_prefix(organization, dataset, split)}/"
        entries = self._extract_task_entries_from_split(split_prefix)

        if not entries:
            return None

        if query:
            q_lower = query.lower()
            entries = [e for e in entries if q_lower in e.name.lower()]

        return _paginate(entries, offset, limit)

    def get_dataset(self, organization: str, dataset: str) -> DatasetInfo | None:
        splits = self.list_dataset_splits(organization, dataset).items
        if not splits:
            return None

        def _count_tasks(split: str) -> tuple[str, int]:
            split_prefix = f"{self._build_prefix(organization, dataset, split)}/"
            return split, len(self._extract_tasks_from_split(split_prefix))

        task_counts: dict[str, int] = {}
        if len(splits) > 1:
            with ThreadPoolExecutor(max_workers=min(len(splits), 10)) as ex:
                for split, count in ex.map(_count_tasks, splits):
                    task_counts[split] = count
        else:
            for s in splits:
                split, count = _count_tasks(s)
                task_counts[split] = count

        return DatasetInfo(id=f"{organization}/{dataset}", splits=splits, task_counts=task_counts)

    def get_task(self, organization: str, dataset: str, split: str, task_id: str) -> TaskInfo | None:
        files = self.list_task_files(organization, dataset, split, task_id).items
        if not files:
            return None
        total_size = sum(f.size for f in files)
        return TaskInfo(
            task_id=task_id,
            dataset_id=f"{organization}/{dataset}",
            split=split,
            files=files,
            total_size=total_size,
        )

    def get_task_metadata(
        self, organization: str, dataset: str, split: str, task_id: str
    ) -> TaskMetadata | None:
        for filename, fmt in _METADATA_CANDIDATES:
            try:
                data = self.read_task_file(organization, dataset, split, task_id, filename)
                content = data.decode("utf-8")
                parsed = None
                if fmt == "json":
                    try:
                        parsed = _json.loads(content)
                    except _json.JSONDecodeError:
                        pass
                return TaskMetadata(source=filename, format=fmt, content=content, parsed=parsed)
            except oss2.exceptions.NoSuchKey:
                continue

        files = self.list_task_files(organization, dataset, split, task_id).items
        if not files:
            return None
        lines = [f"# {task_id}", "", "Files:", ""]
        for f in files:
            lines.append(f"- {f.path} ({f.size} bytes)")
        return TaskMetadata(source="generated", format="markdown", content="\n".join(lines), generated=True)

    def browse_task_files(
        self,
        organization: str,
        dataset: str,
        split: str,
        task_id: str,
        prefix: str = "",
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> PageResult[FileEntry]:
        task_prefix = f"{self._build_prefix(organization, dataset, split)}/{task_id}/"
        browse_prefix = f"{task_prefix}{prefix}" if prefix else task_prefix
        if browse_prefix and not browse_prefix.endswith("/"):
            browse_prefix += "/"

        entries: list[FileEntry] = []
        for entry in self._iter_objects(browse_prefix, delimiter="/"):
            if isinstance(entry, _PrefixEntry):
                dir_name = self._last_segment(entry.key)
                relative = entry.key[len(task_prefix) :].rstrip("/")
                entries.append(FileEntry(name=dir_name, path=relative, type="directory"))
            else:
                if entry.key == browse_prefix or entry.key.endswith("/"):
                    continue
                file_name = entry.key.rsplit("/", 1)[-1]
                relative = entry.key[len(task_prefix) :]
                updated_at = datetime.fromtimestamp(entry.last_modified, tz=timezone.utc).isoformat()
                mt = mimetypes.guess_type(file_name)[0]
                entries.append(
                    FileEntry(
                        name=file_name,
                        path=relative,
                        type="file",
                        size=entry.size,
                        media_type=mt,
                        updated_at=updated_at,
                        etag=entry.etag,
                    )
                )

        entries.sort(key=lambda e: (e.type != "directory", e.name))
        return _paginate(entries, offset, limit)

    def list_task_files(
        self, organization: str, dataset: str, split: str, task_id: str, *, offset: int = 0, limit: int | None = None
    ) -> PageResult[TaskFileInfo]:
        task_prefix = f"{self._build_prefix(organization, dataset, split)}/{task_id}/"
        files: list[TaskFileInfo] = []
        for entry in self._iter_objects(task_prefix):
            if entry.key.endswith("/"):
                continue
            relative = entry.key[len(task_prefix) :]
            last_modified = datetime.fromtimestamp(entry.last_modified, tz=timezone.utc).isoformat()
            files.append(TaskFileInfo(path=relative, size=entry.size, last_modified=last_modified))
        return _paginate(files, offset, limit)

    def read_task_file(self, organization: str, dataset: str, split: str, task_id: str, file_path: str) -> bytes:
        bucket = self._build_bucket()
        key = f"{self._build_prefix(organization, dataset, split)}/{task_id}/{file_path}"
        result = bucket.get_object(key)
        return result.read()

    def download_task_file(
        self, organization: str, dataset: str, split: str, task_id: str, file_path: str, local_path: Path
    ) -> Path:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        bucket = self._build_bucket()
        key = f"{self._build_prefix(organization, dataset, split)}/{task_id}/{file_path}"
        bucket.get_object_to_file(key, str(local_path))
        return local_path

    def download_task(
        self, organization: str, dataset: str, split: str, task_id: str, local_dir: Path, concurrency: int = 4
    ) -> Path:
        files = self.list_task_files(organization, dataset, split, task_id).items
        if not files:
            return local_dir
        task_dir = local_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        def _download_one(fi: TaskFileInfo) -> None:
            self.download_task_file(organization, dataset, split, task_id, fi.path, task_dir / fi.path)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(_download_one, fi): fi for fi in files}
            for future in as_completed(futures):
                fi = futures[future]
                try:
                    future.result()
                except Exception:
                    logger.error("Failed to download %s/%s", task_id, fi.path, exc_info=True)
                    raise
        return task_dir

    def _task_exists(self, bucket: oss2.Bucket, task_prefix: str) -> bool:
        result = bucket.list_objects_v2(prefix=task_prefix, max_keys=1)
        return len(result.object_list) > 0

    def _upload_task(
        self,
        bucket: oss2.Bucket,
        org: str,
        name: str,
        split: str,
        task_dir: Path,
        overwrite: bool,
    ) -> int | None:
        task_id = task_dir.name
        base = self._registry.oss_dataset_path or "datasets"
        task_prefix = f"{base}/{org}/{name}/{split}/{task_id}/"

        if not overwrite and self._task_exists(bucket, task_prefix):
            return None

        files = [f for f in task_dir.rglob("*") if f.is_file()]
        for file in files:
            key = f"{task_prefix}{file.relative_to(task_dir)}"
            bucket.put_object(key, file.read_bytes())
        return len(files)

    def upload_dataset(
        self,
        source: LocalDatasetConfig,
        target: RegistryDatasetConfig,
        concurrency: int = 4,
    ) -> UploadResult:
        org, name = target.name.split("/", 1)
        split = target.version or ""
        overwrite = target.overwrite
        local_dir = source.path

        bucket = self._build_bucket()
        task_dirs = sorted([d for d in local_dir.iterdir() if d.is_dir()])

        raw: dict[str, int | None | Exception] = {}
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(self._upload_task, bucket, org, name, split, d, overwrite): d for d in task_dirs}
            for future, task_dir in futures.items():
                try:
                    raw[task_dir.name] = future.result()
                except Exception as exc:
                    raw[task_dir.name] = exc

        uploaded = skipped = failed = 0
        for task_id in sorted(raw):
            outcome = raw[task_id]
            if isinstance(outcome, Exception):
                failed += 1
                logger.error("Failed to upload task %s: %s", task_id, outcome)
                print(f"  ✗ {task_id}  failed: {outcome}")
            elif outcome is None:
                skipped += 1
                print(f"  - {task_id}  skipped (already exists)")
            else:
                uploaded += 1
                print(f"  ✓ {task_id}  ({outcome} files)")

        return UploadResult(
            id=f"{org}/{name}",
            split=split,
            uploaded=uploaded,
            skipped=skipped,
            failed=failed,
        )
