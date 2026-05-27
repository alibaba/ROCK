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
        elif args.datasets_command == "splits":
            await self._splits(args)
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

        if getattr(args, "org", None):
            datasets = client.list_org_datasets(args.org)
            pairs = [(args.org, d) for d in datasets]
            self._render_org_dataset_pairs(pairs)
            return

        depth = getattr(args, "depth", None) or 2
        if depth == 1:
            orgs = client.list_organizations()
            self._render_orgs(orgs)
            return

        pairs = client.list_all_datasets()
        self._render_org_dataset_pairs(pairs)

    @staticmethod
    def _render_org_dataset_pairs(pairs: list[tuple[str, str]]) -> None:
        if not pairs:
            print("No datasets found.")
            return
        col_org = max(len("Organization"), max(len(o) for o, _ in pairs))
        col_ds = max(len("Dataset"), max(len(d) for _, d in pairs))
        header = f"{'Organization':<{col_org}}  {'Dataset':<{col_ds}}"
        print(header)
        print("-" * len(header))
        for o, d in pairs:
            print(f"{o:<{col_org}}  {d:<{col_ds}}")
        n_orgs = len({o for o, _ in pairs})
        print(f"\n{len(pairs)} datasets in {n_orgs} organizations.")

    @staticmethod
    def _render_orgs(orgs: list[str]) -> None:
        if not orgs:
            print("No organizations found.")
            return
        width = max(len("Organization"), max(len(o) for o in orgs))
        print(f"{'Organization':<{width}}")
        print("-" * width)
        for o in orgs:
            print(o)
        print(f"\n{len(orgs)} organizations.")

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

        print()
        print("=" * 80)
        print(f"Dataset: {spec.id}  Split: {spec.split}  Total: {total}  Shown: {len(shown_task_ids)}")
        print("=" * 80)
        print("#Task name")
        print("-" * 10)
        for task_id in shown_task_ids:
            print(task_id)

    async def _splits(self, args: argparse.Namespace) -> None:
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)
        splits = client.list_dataset_splits(args.org, args.dataset)

        if not splits:
            print(f"No splits found for dataset '{args.org}/{args.dataset}'.")
            return

        width = max(len("Split"), max(len(s) for s in splits))
        print(f"{'Split':<{width}}")
        print("-" * width)
        for s in splits:
            print(s)
        word = "split" if len(splits) == 1 else "splits"
        print(f"\n{len(splits)} {word}.")

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
            parser.add_argument(
                "--access-key-id", dest="access_key_id", help="OSS access key ID (overrides config.ini)"
            )
            parser.add_argument(
                "--access-key-secret", dest="access_key_secret", help="OSS access key secret (overrides config.ini)"
            )
            parser.add_argument("--region", help="OSS region (overrides config.ini)")

        list_parser = datasets_subparsers.add_parser("list", help="List datasets in OSS registry")
        list_group = list_parser.add_mutually_exclusive_group()
        list_group.add_argument(
            "--depth",
            type=int,
            choices=[1, 2],
            default=None,
            help="1: list orgs only. 2 (default): list orgs and datasets.",
        )
        list_group.add_argument("--org", help="List datasets under the given organization only")
        add_oss_args(list_parser)
        tasks_parser = datasets_subparsers.add_parser("tasks", help="List task IDs under one dataset split")
        tasks_parser.add_argument("--org", required=True, help="Organization name")
        tasks_parser.add_argument("--dataset", required=True, help="Dataset name")
        tasks_parser.add_argument("--split", default="test", help="Split name (default: test)")
        tasks_parser.add_argument("--offset", type=_non_negative_int, default=0, help="Skip first N tasks")
        tasks_parser.add_argument("--limit", type=_positive_int, default=None, help="Maximum number of tasks to show")
        add_oss_args(tasks_parser)

        splits_parser = datasets_subparsers.add_parser("splits", help="List splits under one dataset")
        splits_parser.add_argument("--org", required=True, help="Organization name")
        splits_parser.add_argument("--dataset", required=True, help="Dataset name")
        add_oss_args(splits_parser)

        upload_parser = datasets_subparsers.add_parser("upload", help="Upload local task dirs to OSS")
        upload_parser.add_argument("--org", required=True, help="Organization name")
        upload_parser.add_argument("--dataset", required=True, help="Dataset name")
        upload_parser.add_argument("--split", required=True, help="Split name (e.g. train, test, v1.0)")
        upload_parser.add_argument("--dir", required=True, help="Local directory containing {task_id}/ subdirectories")
        upload_parser.add_argument(
            "--concurrency",
            type=int,
            default=4,
            choices=range(1, 17),
            metavar="[1-16]",
            help="Upload concurrency (default: 4)",
        )
        upload_parser.add_argument(
            "--overwrite", action="store_true", help="Overwrite existing tasks in OSS (default: skip)"
        )
        add_oss_args(upload_parser)
