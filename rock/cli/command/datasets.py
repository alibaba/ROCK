from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rock.cli.command.command import Command
from rock.cli.config import ConfigManager
from rock.logger import init_logger
from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.datasets.client import DatasetClient

logger = init_logger(__name__)


def _non_negative_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return ivalue


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError("must be >= 1")
    return ivalue


class DatasetsCommand(Command):
    name = "datasets"

    async def arun(self, args: argparse.Namespace) -> None:
        if args.datasets_command == "list":
            await self._list(args)
        elif args.datasets_command == "tasks":
            await self._tasks(args)
        elif args.datasets_command == "upload":
            await self._upload(args)
        else:
            raise ValueError(f"Unknown datasets command: {args.datasets_command}")

    def _build_oss_registry_info(self, args: argparse.Namespace) -> OssRegistryInfo:
        ds_cfg = ConfigManager(Path(args.config) if args.config else None).get_config().dataset_config

        bucket = getattr(args, "bucket", None) or ds_cfg.oss_bucket
        if not bucket:
            raise ValueError(
                "OSS bucket is required. Pass --bucket or set 'oss_bucket' in [dataset] section of config.ini."
            )
        return OssRegistryInfo(
            oss_bucket=bucket,
            oss_endpoint=getattr(args, "endpoint", None) or ds_cfg.oss_endpoint,
            oss_access_key_id=getattr(args, "access_key_id", None) or ds_cfg.oss_access_key_id,
            oss_access_key_secret=getattr(args, "access_key_secret", None) or ds_cfg.oss_access_key_secret,
            oss_region=getattr(args, "region", None) or ds_cfg.oss_region,
        )

    async def _list(self, args: argparse.Namespace) -> None:
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)
        datasets = client.list_datasets(org=getattr(args, "org", None))

        if not datasets:
            print("No datasets found.")
            return

        col_id = max(len("Dataset"), max(len(d.id) for d in datasets))
        col_split = max(len("Split"), max(len(d.split) for d in datasets))

        header = f"{'Dataset':<{col_id}}  {'Split':<{col_split}}  {'Tasks':>6}"
        print(header)
        print("-" * len(header))
        for ds in sorted(datasets, key=lambda d: (d.id, d.split)):
            print(f"{ds.id:<{col_id}}  {ds.split:<{col_split}}  {len(ds.task_ids):>6}")

    async def _tasks(self, args: argparse.Namespace) -> None:
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)
        spec = client.list_dataset_tasks(args.org, args.dataset, args.split)

        if spec is None or not spec.task_ids:
            print(f"No tasks found for dataset '{args.org}/{args.dataset}' split '{args.split}'.")
            return

        total = len(spec.task_ids)
        start = args.offset
        end = start + args.limit if args.limit is not None else None
        shown_task_ids = spec.task_ids[start:end]

        if not shown_task_ids:
            print("No tasks found after applying offset/limit.")
            return

        limit_text = str(args.limit) if args.limit is not None else "all"

        print()
        print("=" * 80)
        print(f"Dataset: {spec.id}  Split: {spec.split}  Total: {total}  Shown: {len(shown_task_ids)}")
        print("=" * 80)
        print("#Task name")
        print("-" * 10)
        for task_id in shown_task_ids:
            print(task_id)

    async def _upload(self, args: argparse.Namespace) -> None:
        local_dir = Path(args.dir)
        if not local_dir.is_dir():
            raise ValueError(f"--dir '{local_dir}' does not exist or is not a directory")

        registry_info = self._build_oss_registry_info(args)
        source = LocalDatasetConfig(path=local_dir)
        target = RegistryDatasetConfig(
            name=f"{args.org}/{args.dataset}",
            version=args.split,
            overwrite=args.overwrite,
            registry=registry_info,
        )

        base = registry_info.oss_dataset_path or "datasets"
        print(f"Uploading to oss://{registry_info.oss_bucket}/{base}/{args.org}/{args.dataset}/{args.split}/")

        client = DatasetClient(registry_info)
        result = client.upload_dataset(source, target, concurrency=args.concurrency)

        print(f"\nDone: {result.uploaded} uploaded, {result.skipped} skipped, {result.failed} failed")
        if result.failed > 0:
            sys.exit(1)

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction) -> None:
        datasets_parser = subparsers.add_parser("datasets", description="Dataset operations on OSS")
        datasets_subparsers = datasets_parser.add_subparsers(dest="datasets_command")

        def add_oss_args(parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--bucket", help="OSS bucket name (overrides config.ini)")
            parser.add_argument("--endpoint", help="OSS endpoint URL (overrides config.ini)")
            parser.add_argument("--access-key-id", dest="access_key_id",
                                help="OSS access key ID (overrides config.ini)")
            parser.add_argument("--access-key-secret", dest="access_key_secret",
                                help="OSS access key secret (overrides config.ini)")
            parser.add_argument("--region", help="OSS region (overrides config.ini)")

        list_parser = datasets_subparsers.add_parser("list", help="List datasets in OSS registry")
        list_parser.add_argument("--org", help="Filter by organization")
        add_oss_args(list_parser)
        tasks_parser = datasets_subparsers.add_parser("tasks", help="List task IDs under one dataset split")
        tasks_parser.add_argument("--org", required=True, help="Organization name")
        tasks_parser.add_argument("--dataset", required=True, help="Dataset name")
        tasks_parser.add_argument("--split", default="test", help="Split name (default: test)")
        tasks_parser.add_argument("--offset", type=_non_negative_int, default=0, help="Skip first N tasks")
        tasks_parser.add_argument("--limit", type=_positive_int, default=None, help="Maximum number of tasks to show")
        add_oss_args(tasks_parser)

        upload_parser = datasets_subparsers.add_parser("upload", help="Upload local task dirs to OSS")
        upload_parser.add_argument("--org", required=True, help="Organization name")
        upload_parser.add_argument("--dataset", required=True, help="Dataset name")
        upload_parser.add_argument("--split", required=True, help="Split name (e.g. train, test, v1.0)")
        upload_parser.add_argument("--dir", required=True,
                                   help="Local directory containing {task_id}/ subdirectories")
        upload_parser.add_argument("--concurrency", type=int, default=4,
                                   choices=range(1, 17), metavar="[1-16]",
                                   help="Upload concurrency (default: 4)")
        upload_parser.add_argument("--overwrite", action="store_true",
                                   help="Overwrite existing tasks in OSS (default: skip)")
        add_oss_args(upload_parser)
