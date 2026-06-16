from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import oss2

from rock.logger import init_logger
from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.datasets.models import DatasetSpec, TaskFile, UploadResult
from rock.sdk.envhub.datasets.registry.base import BaseDatasetRegistry

logger = init_logger(__name__)


class OssDatasetRegistry(BaseDatasetRegistry):
    def __init__(self, registry: OssRegistryInfo) -> None:
        self._registry = registry
        self._bucket: oss2.Bucket | None = None

    def _build_bucket(self) -> oss2.Bucket:
        if self._bucket is None:
            auth = oss2.Auth(
                self._registry.oss_access_key_id or "",
                self._registry.oss_access_key_secret or "",
            )
            self._bucket = oss2.Bucket(auth, self._registry.oss_endpoint or "", self._registry.oss_bucket)
        return self._bucket

    def _build_prefix(self, org: str, name: str, split: str | None = None) -> str:
        base = self._registry.oss_dataset_path or "datasets"
        parts = [base, org, name]
        if split:
            parts.append(split)
        return "/".join(parts)

    def _build_task_prefix(self, org: str, name: str, split: str, task_id: str) -> str:
        return f"{self._build_prefix(org, name, split)}/{task_id}/"

    @staticmethod
    def _last_segment(prefix: str) -> str:
        return prefix.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _normalize_task_path(path: str, *, allow_empty: bool = False, directory: bool = False) -> str:
        raw = (path or "").replace("\\", "/")
        if raw.startswith("/"):
            raise ValueError("relative task path must not be absolute")

        parts = [p for p in raw.split("/") if p and p != "."]
        if any(p == ".." for p in parts):
            raise ValueError("relative task path must not contain '..'")
        if not parts:
            if allow_empty:
                return ""
            raise ValueError("relative task path is required")

        normalized = "/".join(parts)
        if directory or raw.endswith("/"):
            normalized += "/"
        return normalized

    @staticmethod
    def _list_objects_v2_pages(bucket: oss2.Bucket, **kwargs):
        """Yield successive list_objects_v2 pages following the continuation token.

        The continuation token is returned by OSS on each truncated response and
        fed back into the next request. The loop terminates when OSS reports no
        more pages (``is_truncated`` is false) or when the token is empty or
        stops advancing -- the latter guards against an infinite loop if OSS (or
        a mock) keeps reporting truncation without a progressing token.
        """
        token = ""
        while True:
            page_kwargs = dict(kwargs)
            if token:
                page_kwargs["continuation_token"] = token
            result = bucket.list_objects_v2(**page_kwargs)
            yield result
            if not getattr(result, "is_truncated", False):
                break
            next_token = getattr(result, "next_continuation_token", "") or ""
            if not next_token or next_token == token:
                break
            token = next_token

    def _extract_tasks_from_split(
        self,
        bucket: oss2.Bucket,
        split_prefix: str,
        *,
        max_items: int | None = None,
        task_filter: str | None = None,
    ) -> list[str]:
        """Extract tasks from a split prefix, combining directory and file tasks.

        Directory tasks: from prefix_list (e.g., "datasets/org/name/split/task-001/")
        File tasks: from object_list (e.g., "datasets/org/name/split/task-001.json")

        File tasks are stripped of their suffix (e.g., "task-001.json" -> "task-001").
        Placeholder objects (key ending with "/") and nested objects are ignored.

        When *task_filter* is set, only tasks whose name starts with the filter
        string are returned (pushed down to the OSS prefix for efficiency).

        Pagination stops early once *max_items* distinct tasks have been
        collected, so a bounded query (``--limit``) does not scan the whole split.
        """
        query_prefix = f"{split_prefix}{task_filter}" if task_filter else split_prefix
        tasks: set[str] = set()

        for result in self._list_objects_v2_pages(bucket, prefix=query_prefix, delimiter="/", max_keys=1000):
            for p in result.prefix_list:
                s = self._last_segment(p)
                if not s.startswith("."):
                    tasks.add(s)

            for obj in result.object_list:
                key = obj.key
                if key.endswith("/"):
                    continue
                relative = key[len(split_prefix) :]
                if "/" in relative or relative.startswith("."):
                    continue
                name = relative.rsplit(".", 1)[0] if "." in relative else relative
                tasks.add(name)

            if max_items is not None and len(tasks) >= max_items:
                break

        return sorted(tasks)

    def list_organizations(self) -> list[str]:
        bucket = self._build_bucket()
        base = self._registry.oss_dataset_path or "datasets"
        prefixes = []
        for result in self._list_objects_v2_pages(bucket, prefix=f"{base}/", delimiter="/", max_keys=1000):
            prefixes.extend(result.prefix_list)
        return sorted(s for p in prefixes if not (s := self._last_segment(p)).startswith("."))

    def list_org_datasets(self, organization: str) -> list[str]:
        bucket = self._build_bucket()
        base = self._registry.oss_dataset_path or "datasets"
        prefixes = []
        for result in self._list_objects_v2_pages(
            bucket, prefix=f"{base}/{organization}/", delimiter="/", max_keys=1000
        ):
            prefixes.extend(result.prefix_list)
        return sorted(s for p in prefixes if not (s := self._last_segment(p)).startswith("."))

    def list_dataset_splits(self, organization: str, dataset: str) -> list[str]:
        bucket = self._build_bucket()
        base = self._registry.oss_dataset_path or "datasets"
        prefixes = []
        for result in self._list_objects_v2_pages(
            bucket, prefix=f"{base}/{organization}/{dataset}/", delimiter="/", max_keys=1000
        ):
            prefixes.extend(result.prefix_list)
        return sorted(s for p in prefixes if not (s := self._last_segment(p)).startswith("."))

    def list_all_datasets(self, concurrency: int = 10) -> list[tuple[str, str]]:
        orgs = self.list_organizations()
        if not orgs:
            return []
        pairs: list[tuple[str, str]] = []
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            future_to_org = {ex.submit(self.list_org_datasets, o): o for o in orgs}
            for fut in as_completed(future_to_org):
                org = future_to_org[fut]
                for ds in fut.result():
                    pairs.append((org, ds))
        return sorted(pairs)

    def list_datasets(self, organization: str | None = None) -> list[DatasetSpec]:
        bucket = self._build_bucket()
        base = self._registry.oss_dataset_path or "datasets"

        if organization:
            org_prefixes = [f"{base}/{organization}/"]
        else:
            org_prefixes = []
            for result in self._list_objects_v2_pages(bucket, prefix=f"{base}/", delimiter="/", max_keys=1000):
                org_prefixes.extend(result.prefix_list)

        datasets: list[DatasetSpec] = []
        for org_prefix in org_prefixes:
            org = self._last_segment(org_prefix)
            if org.startswith("."):
                continue

            name_prefixes = []
            for result in self._list_objects_v2_pages(bucket, prefix=org_prefix, delimiter="/", max_keys=1000):
                name_prefixes.extend(result.prefix_list)
            for name_prefix in name_prefixes:
                name = self._last_segment(name_prefix)
                if name.startswith("."):
                    continue

                split_prefixes = []
                for result2 in self._list_objects_v2_pages(bucket, prefix=name_prefix, delimiter="/", max_keys=1000):
                    split_prefixes.extend(result2.prefix_list)
                for split_prefix in split_prefixes:
                    split = self._last_segment(split_prefix)
                    if split.startswith("."):
                        continue

                    task_ids = self._extract_tasks_from_split(bucket, split_prefix)
                    datasets.append(
                        DatasetSpec(
                            id=f"{org}/{name}",
                            split=split,
                            task_ids=task_ids,
                        )
                    )

        return datasets

    def list_dataset_tasks(
        self,
        organization: str,
        dataset: str,
        split: str = "test",
        *,
        offset: int = 0,
        limit: int | None = None,
        task_filter: str | None = None,
    ) -> DatasetSpec | None:
        bucket = self._build_bucket()
        split_prefix = f"{self._build_prefix(organization, dataset, split)}/"
        max_items = offset + limit if limit is not None else None
        task_ids = self._extract_tasks_from_split(bucket, split_prefix, max_items=max_items, task_filter=task_filter)

        if not task_ids:
            return None

        return DatasetSpec(
            id=f"{organization}/{dataset}",
            split=split,
            task_ids=task_ids[offset:max_items],
        )

    def list_task_files(
        self, organization: str, dataset: str, split: str, task_id: str, path: str = ""
    ) -> list[TaskFile]:
        bucket = self._build_bucket()
        task_prefix = self._build_task_prefix(organization, dataset, split, task_id)
        relative_prefix = self._normalize_task_path(path, allow_empty=True, directory=bool(path)) if path else ""

        files: list[TaskFile] = []
        for result in self._list_objects_v2_pages(bucket, prefix=f"{task_prefix}{relative_prefix}", max_keys=1000):
            for obj in result.object_list:
                key = obj.key
                if key.endswith("/") or not key.startswith(task_prefix):
                    continue
                relative = key[len(task_prefix) :]
                if not relative:
                    continue
                files.append(TaskFile(path=relative, size=getattr(obj, "size", None)))
        if not files and not relative_prefix:
            split_prefix = f"{self._build_prefix(organization, dataset, split)}/"
            for result in self._list_objects_v2_pages(bucket, prefix=f"{split_prefix}{task_id}", max_keys=1000):
                for obj in result.object_list:
                    key = obj.key
                    if key.endswith("/") or not key.startswith(split_prefix):
                        continue
                    relative = key[len(split_prefix) :]
                    if "/" in relative:
                        continue
                    name = relative.rsplit(".", 1)[0] if "." in relative else relative
                    if name == task_id:
                        files.append(TaskFile(path=relative, size=getattr(obj, "size", None)))
        return sorted(files, key=lambda f: f.path)

    def get_task_file(self, organization: str, dataset: str, split: str, task_id: str, path: str) -> bytes | None:
        bucket = self._build_bucket()
        relative = self._normalize_task_path(path)
        key = f"{self._build_task_prefix(organization, dataset, split, task_id)}{relative}"
        try:
            return bucket.get_object(key).read()
        except (oss2.exceptions.NoSuchKey, oss2.exceptions.NotFound):
            if "/" not in relative:
                direct_name = relative.rsplit(".", 1)[0] if "." in relative else relative
                if direct_name == task_id:
                    direct_key = f"{self._build_prefix(organization, dataset, split)}/{relative}"
                    try:
                        return bucket.get_object(direct_key).read()
                    except (oss2.exceptions.NoSuchKey, oss2.exceptions.NotFound):
                        return None
            return None

    def _task_exists(self, bucket: oss2.Bucket, task_prefix: str) -> bool:
        result = bucket.list_objects_v2(prefix=task_prefix, max_keys=1)
        return len(result.object_list) > 0

    def _object_exists(self, bucket: oss2.Bucket, key: str) -> bool:
        result = bucket.list_objects_v2(prefix=key, max_keys=1)
        return any(obj.key == key for obj in result.object_list)

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

    def _upload_task_file(
        self,
        bucket: oss2.Bucket,
        org: str,
        name: str,
        split: str,
        task_file: Path,
        overwrite: bool,
    ) -> int | None:
        key = f"{self._build_prefix(org, name, split)}/{task_file.name}"

        if not overwrite and self._object_exists(bucket, key):
            return None

        bucket.put_object(key, task_file.read_bytes())
        return 1

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
        if local_dir.is_file():
            upload_items = [local_dir]
        else:
            upload_items = sorted([p for p in local_dir.iterdir() if p.is_dir() or p.is_file()])

        raw: dict[str, int | None | Exception] = {}
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {}
            for item in upload_items:
                if item.is_dir():
                    future = executor.submit(self._upload_task, bucket, org, name, split, item, overwrite)
                else:
                    future = executor.submit(self._upload_task_file, bucket, org, name, split, item, overwrite)
                futures[future] = item
            for future, item in futures.items():
                try:
                    raw[item.name] = future.result()
                except Exception as exc:
                    raw[item.name] = exc

        uploaded = skipped = failed = 0
        for task_id in sorted(raw):
            outcome = raw[task_id]
            if isinstance(outcome, Exception):
                failed += 1
                logger.error("Failed to upload task %s: %s", task_id, outcome)
                print(f"  \u2717 {task_id}  failed: {outcome}")
            elif outcome is None:
                skipped += 1
                print(f"  - {task_id}  skipped (already exists)")
            else:
                uploaded += 1
                print(f"  \u2713 {task_id}  ({outcome} files)")

        return UploadResult(
            id=f"{org}/{name}",
            split=split,
            uploaded=uploaded,
            skipped=skipped,
            failed=failed,
        )
