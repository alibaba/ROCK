from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import oss2

from rock.logger import init_logger
from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.datasets.models import DatasetSpec, UploadResult
from rock.sdk.envhub.datasets.registry.base import BaseDatasetRegistry

logger = init_logger(__name__)

# Hard upper bound on pagination pages. At 1000 keys/page this covers 10M keys,
# far beyond any real split, while guaranteeing the loop always terminates even
# if OSS (or a mock) keeps reporting truncation with a non-advancing token.
_MAX_PAGINATION_PAGES = 10_000


@dataclass
class _PaginationCache:
    split_prefix: str = ""
    tasks: list[str] = field(default_factory=list)
    continuation_token: str = ""
    is_exhausted: bool = False


class OssDatasetRegistry(BaseDatasetRegistry):
    def __init__(self, registry: OssRegistryInfo) -> None:
        self._registry = registry
        self._bucket: oss2.Bucket | None = None
        self._page_cache = _PaginationCache()

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

    @staticmethod
    def _last_segment(prefix: str) -> str:
        return prefix.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _list_objects_v2_pages(bucket: oss2.Bucket, **kwargs):
        token = ""
        for _ in range(_MAX_PAGINATION_PAGES):
            page_kwargs = dict(kwargs)
            if token:
                page_kwargs["continuation_token"] = token
            result = bucket.list_objects_v2(**page_kwargs)
            yield result
            if not getattr(result, "is_truncated", False):
                break
            next_token = getattr(result, "next_continuation_token", "") or ""
            # Stop if the token is empty or fails to advance (would loop forever).
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

        Uses an internal pagination cache: if the same query is repeated, results
        are served from cache or resumed via continuation token.
        """
        query_prefix = f"{split_prefix}{task_filter}" if task_filter else split_prefix
        cache = self._page_cache

        # Cache hit: same query
        if cache.split_prefix == query_prefix:
            if cache.is_exhausted or (max_items is not None and len(cache.tasks) >= max_items):
                return cache.tasks[:max_items] if max_items else list(cache.tasks)
            tasks_set: set[str] = set(cache.tasks)
            token = cache.continuation_token
        else:
            tasks_set = set()
            token = ""

        for _ in range(_MAX_PAGINATION_PAGES):
            mk = 1000
            if max_items is not None:
                mk = min(1000, max(max_items - len(tasks_set), 100))

            kwargs: dict = {"prefix": query_prefix, "delimiter": "/", "max_keys": mk}
            if token:
                kwargs["continuation_token"] = token

            result = bucket.list_objects_v2(**kwargs)

            for p in result.prefix_list:
                s = self._last_segment(p)
                if not s.startswith("."):
                    tasks_set.add(s)

            for obj in result.object_list:
                key = obj.key
                if key.endswith("/"):
                    continue
                relative = key[len(split_prefix) :]
                if "/" in relative or relative.startswith("."):
                    continue
                name = relative.rsplit(".", 1)[0] if "." in relative else relative
                tasks_set.add(name)

            is_truncated = getattr(result, "is_truncated", False)
            next_token = getattr(result, "next_continuation_token", "") or ""

            if not is_truncated or not next_token:
                sorted_tasks = sorted(tasks_set)
                cache.split_prefix = query_prefix
                cache.tasks = sorted_tasks
                cache.continuation_token = ""
                cache.is_exhausted = True
                return sorted_tasks[:max_items] if max_items else sorted_tasks

            if max_items is not None and len(tasks_set) >= max_items:
                sorted_tasks = sorted(tasks_set)
                cache.split_prefix = query_prefix
                cache.tasks = sorted_tasks
                cache.continuation_token = next_token
                cache.is_exhausted = False
                return sorted_tasks[:max_items]

            # Guard against a non-advancing continuation token: if OSS keeps
            # returning the same token we would otherwise spin forever.
            if next_token == token:
                break
            token = next_token

        # Page budget exhausted (or token stopped advancing): return what we
        # have rather than looping forever.
        logger.warning(
            "Pagination stopped after %d pages for prefix %r; returning partial results",
            _MAX_PAGINATION_PAGES,
            query_prefix,
        )
        sorted_tasks = sorted(tasks_set)
        cache.split_prefix = query_prefix
        cache.tasks = sorted_tasks
        cache.continuation_token = ""
        cache.is_exhausted = True
        return sorted_tasks[:max_items] if max_items else sorted_tasks

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
