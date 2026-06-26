from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rock.cli.command.command import Command
from rock.cli.config import ConfigManager
from rock.logger import init_logger
from rock.sdk.bench.models.job.config import LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig
from rock.sdk.envhub.datasets.client import DatasetClient
from rock.sdk.envhub.datasets.models import PageResult

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


def _page_summary(page: PageResult) -> str:
    shown = len(page.items)
    if page.offset == 0 and (page.limit is None or shown < page.limit):
        return f"{page.total} total."
    start = page.offset + 1
    end = page.offset + shown
    return f"Showing {start}-{end} of {page.total}."


class DatasetsCommand(Command):
    name = "datasets"

    async def arun(self, args: argparse.Namespace) -> None:
        cmd = args.datasets_command
        if cmd == "list":
            await self._list(args)
        elif cmd == "info":
            await self._info(args)
        elif cmd == "tasks":
            await self._tasks(args)
        elif cmd == "splits":
            await self._splits(args)
        elif cmd == "files":
            await self._files(args)
        elif cmd == "cat":
            await self._cat(args)
        elif cmd == "download":
            await self._download(args)
        elif cmd == "upload":
            await self._upload(args)
        else:
            raise ValueError(f"Unknown datasets command: {cmd}")

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
        offset = args.offset
        limit = args.limit

        if getattr(args, "org", None):
            page = client.list_org_datasets(args.org, offset=offset, limit=limit)
            pairs = [(args.org, d) for d in page.items]
            self._render_org_dataset_pairs(pairs)
            print(f"\n{_page_summary(page)}")
            return

        depth = getattr(args, "depth", None) or 2
        if depth == 1:
            page = client.list_organizations(offset=offset, limit=limit)
            self._render_orgs(page.items)
            print(f"\n{_page_summary(page)}")
            return

        page = client.list_all_datasets(offset=offset, limit=limit)
        self._render_org_dataset_pairs(page.items)
        print(f"\n{_page_summary(page)}")

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

    async def _tasks(self, args: argparse.Namespace) -> None:
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)
        page = client.list_dataset_tasks(args.org, args.dataset, args.split, offset=args.offset, limit=args.limit)

        if page is None or not page.items:
            print(f"No tasks found for dataset '{args.org}/{args.dataset}' split '{args.split}'.")
            return

        print()
        print("=" * 80)
        print(f"Dataset: {args.org}/{args.dataset}  Split: {args.split}  {_page_summary(page)}")
        print("=" * 80)
        print("#Task name")
        print("-" * 10)
        for task_id in page.items:
            print(task_id)

    async def _splits(self, args: argparse.Namespace) -> None:
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)
        page = client.list_dataset_splits(args.org, args.dataset, offset=args.offset, limit=args.limit)

        if not page.items:
            print(f"No splits found for dataset '{args.org}/{args.dataset}'.")
            return

        width = max(len("Split"), max(len(s) for s in page.items))
        print(f"{'Split':<{width}}")
        print("-" * width)
        for s in page.items:
            print(s)
        print(f"\n{_page_summary(page)}")

    async def _info(self, args: argparse.Namespace) -> None:
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)
        info = client.get_dataset(args.org, args.dataset)

        if info is None:
            print(f"Dataset '{args.org}/{args.dataset}' not found.")
            return

        print(f"\nDataset: {info.id}")
        print(f"Splits:  {len(info.splits)}")
        print()
        col_split = max(len("Split"), max(len(s) for s in info.splits))
        col_tasks = len("Tasks")
        header = f"{'Split':<{col_split}}  {'Tasks':>{col_tasks}}"
        print(header)
        print("-" * len(header))
        for s in info.splits:
            count = info.task_counts.get(s, 0)
            print(f"{s:<{col_split}}  {count:>{col_tasks}}")
        total = sum(info.task_counts.values())
        print(f"\nTotal: {total} tasks across {len(info.splits)} splits.")

    async def _files(self, args: argparse.Namespace) -> None:
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)
        page = client.list_task_files(
            args.org, args.dataset, args.split, args.task, offset=args.offset, limit=args.limit
        )

        if not page.items:
            print(f"No files found for task '{args.task}' in '{args.org}/{args.dataset}' split '{args.split}'.")
            return

        files = page.items
        col_path = max(len("Path"), max(len(f.path) for f in files))
        col_size = max(len("Size"), max(len(str(f.size)) for f in files))
        header = f"{'Path':<{col_path}}  {'Size':>{col_size}}  Last Modified"
        print(header)
        print("-" * len(header))
        for f in files:
            print(f"{f.path:<{col_path}}  {f.size:>{col_size}}  {f.last_modified}")
        total_size = sum(f.size for f in files)
        print(f"\n{_page_summary(page)} Total size: {total_size} bytes.")

    async def _cat(self, args: argparse.Namespace) -> None:
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)
        data = client.read_task_file(args.org, args.dataset, args.split, args.task, args.file)
        sys.stdout.buffer.write(data)

    async def _download(self, args: argparse.Namespace) -> None:
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)
        local_dir = Path(args.dir)

        file_path = getattr(args, "file", None)
        if file_path:
            dest = local_dir / file_path
            client.download_task_file(args.org, args.dataset, args.split, args.task, file_path, dest)
            print(f"Downloaded: {dest}")
        else:
            task_dir = client.download_task(
                args.org, args.dataset, args.split, args.task, local_dir, concurrency=args.concurrency
            )
            print(f"Downloaded task '{args.task}' to {task_dir}")

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

        def add_page_args(parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--offset", type=_non_negative_int, default=0, help="Skip first N items (default: 0)")
            parser.add_argument("--limit", type=_positive_int, default=None, help="Maximum number of items to show")

        # rock datasets info
        info_parser = datasets_subparsers.add_parser("info", help="Show dataset details (splits and task counts)")
        info_parser.add_argument("--org", required=True, help="Organization name")
        info_parser.add_argument("--dataset", required=True, help="Dataset name")
        add_oss_args(info_parser)

        # rock datasets list
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
        add_page_args(list_parser)

        # rock datasets tasks
        tasks_parser = datasets_subparsers.add_parser("tasks", help="List task IDs under one dataset split")
        tasks_parser.add_argument("--org", required=True, help="Organization name")
        tasks_parser.add_argument("--dataset", required=True, help="Dataset name")
        tasks_parser.add_argument("--split", default="test", help="Split name (default: test)")
        add_oss_args(tasks_parser)
        add_page_args(tasks_parser)

        # rock datasets splits
        splits_parser = datasets_subparsers.add_parser("splits", help="List splits under one dataset")
        splits_parser.add_argument("--org", required=True, help="Organization name")
        splits_parser.add_argument("--dataset", required=True, help="Dataset name")
        add_oss_args(splits_parser)
        add_page_args(splits_parser)

        # rock datasets files
        files_parser = datasets_subparsers.add_parser("files", help="List files under a task")
        files_parser.add_argument("--org", required=True, help="Organization name")
        files_parser.add_argument("--dataset", required=True, help="Dataset name")
        files_parser.add_argument("--split", default="test", help="Split name (default: test)")
        files_parser.add_argument("--task", required=True, help="Task ID")
        add_oss_args(files_parser)
        add_page_args(files_parser)

        # rock datasets cat
        cat_parser = datasets_subparsers.add_parser("cat", help="Print file content to stdout")
        cat_parser.add_argument("--org", required=True, help="Organization name")
        cat_parser.add_argument("--dataset", required=True, help="Dataset name")
        cat_parser.add_argument("--split", default="test", help="Split name (default: test)")
        cat_parser.add_argument("--task", required=True, help="Task ID")
        cat_parser.add_argument("--file", required=True, help="File path relative to task directory")
        add_oss_args(cat_parser)

        # rock datasets download
        download_parser = datasets_subparsers.add_parser("download", help="Download task files to local directory")
        download_parser.add_argument("--org", required=True, help="Organization name")
        download_parser.add_argument("--dataset", required=True, help="Dataset name")
        download_parser.add_argument("--split", default="test", help="Split name (default: test)")
        download_parser.add_argument("--task", required=True, help="Task ID")
        download_parser.add_argument("--file", required=False, help="Download a single file (omit to download all)")
        download_parser.add_argument("--dir", required=True, help="Local directory to download into")
        download_parser.add_argument(
            "--concurrency",
            type=int,
            default=4,
            choices=range(1, 17),
            metavar="[1-16]",
            help="Download concurrency (default: 4)",
        )
        add_oss_args(download_parser)

        # rock datasets upload
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
