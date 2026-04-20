from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import oss2

from rock.logger import init_logger
from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.datasets.models import DatasetSpec, UploadResult
from rock.sdk.envhub.datasets.registry.base import BaseDatasetRegistry

logger = init_logger(__name__)


class OssDatasetRegistry(BaseDatasetRegistry):

    def __init__(self, registry: OssRegistryInfo) -> None:
        self._registry = registry

    def _build_bucket(self) -> oss2.Bucket:
        auth = oss2.Auth(
            self._registry.oss_access_key_id or "",
            self._registry.oss_access_key_secret or "",
        )
        return oss2.Bucket(auth, self._registry.oss_endpoint or "", self._registry.oss_bucket)

    def _build_prefix(self, org: str, name: str, split: str | None = None) -> str:
        base = self._registry.oss_dataset_path or "datasets"
        parts = [base, org, name]
        if split:
            parts.append(split)
        return "/".join(parts)

    @staticmethod
    def _last_segment(prefix: str) -> str:
        return prefix.rstrip("/").rsplit("/", 1)[-1]

    def list_datasets(self, organization: str | None = None) -> list[DatasetSpec]:
        bucket = self._build_bucket()
        base = self._registry.oss_dataset_path or "datasets"

        if organization:
            org_prefixes = [f"{base}/{organization}/"]
        else:
            result = bucket.list_objects_v2(prefix=f"{base}/", delimiter="/", max_keys=1000)
            org_prefixes = result.prefix_list

        datasets: list[DatasetSpec] = []
        for org_prefix in org_prefixes:
            org = self._last_segment(org_prefix)

            result = bucket.list_objects_v2(prefix=org_prefix, delimiter="/", max_keys=1000)
            for name_prefix in result.prefix_list:
                name = self._last_segment(name_prefix)

                result2 = bucket.list_objects_v2(prefix=name_prefix, delimiter="/", max_keys=1000)
                for split_prefix in result2.prefix_list:
                    split = self._last_segment(split_prefix)

                    result3 = bucket.list_objects_v2(prefix=split_prefix, delimiter="/", max_keys=1000)
                    task_ids = [self._last_segment(p) for p in result3.prefix_list]
                    datasets.append(DatasetSpec(
                        organization=org,
                        name=name,
                        split=split,
                        task_ids=task_ids,
                    ))

        return datasets

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
            futures = {
                executor.submit(self._upload_task, bucket, org, name, split, d, overwrite): d
                for d in task_dirs
            }
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
            organization=org,
            name=name,
            split=split,
            uploaded=uploaded,
            skipped=skipped,
            failed=failed,
        )
