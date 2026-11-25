from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import asdict
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


def _is_json_output(args: argparse.Namespace) -> bool:
    return getattr(args, "output", None) == "json"


def _print_json(data: dict | list) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _dataset_to_dict(dataset) -> dict:
    data = asdict(dataset)
    data["task_count"] = len(dataset.task_ids)
    return data


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


def _write_stdout_bytes(content: bytes) -> None:
    try:
        sys.stdout.write(content.decode("utf-8"))
    except UnicodeDecodeError:
        sys.stdout.buffer.write(content)


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
        elif args.datasets_command in ("fs", "files"):
            await self._fs(args)
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

        if _is_json_output(args):
            if getattr(args, "depth", None) == 1:
                _print_json({"organizations": client.list_organizations()})
                return
            datasets = sorted(
                client.list_datasets(getattr(args, "org", None)),
                key=lambda d: (d.id, d.split),
            )
            _print_json({"datasets": [_dataset_to_dict(d) for d in datasets]})
            return

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
        spec = client.list_dataset_tasks(
            args.org, args.dataset, args.split,
            offset=args.offset, limit=args.limit, task_filter=getattr(args, "filter", None),
        )

        if spec is None or not spec.task_ids:
            if _is_json_output(args):
                _print_json({
                    "dataset": f"{args.org}/{args.dataset}",
                    "split": args.split,
                    "total": 0,
                    "offset": args.offset,
                    "limit": args.limit,
                    "task_ids": [],
                })
                return
            print(f"No tasks found for dataset '{args.org}/{args.dataset}' split '{args.split}'.")
            return

        shown_task_ids = spec.task_ids

        if _is_json_output(args):
            _print_json({
                "dataset": spec.id,
                "split": spec.split,
                "total": len(shown_task_ids),
                "offset": args.offset,
                "limit": args.limit,
                "task_ids": shown_task_ids,
            })
            return

        if not shown_task_ids:
            print("No tasks found after applying offset/limit.")
            return

        print()
        print("=" * 80)
        print(f"Dataset: {spec.id}  Split: {spec.split}  Shown: {len(shown_task_ids)}")
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
        if not local_dir.exists():
            raise ValueError(f"--dir '{local_dir}' does not exist")

        registry_info = self._build_oss_registry_info(args)
        source = LocalDatasetConfig(path=local_dir)
        target = RegistryDatasetConfig(
            name=f"{args.org}/{args.dataset}",
            version=args.split,
            overwrite=args.overwrite,
            registry=registry_info,
        )

        base = registry_info.oss_dataset_path or "datasets"
        if not _is_json_output(args):
            print(f"Uploading to oss://{registry_info.oss_bucket}/{base}/{args.org}/{args.dataset}/{args.split}/")

        client = DatasetClient(registry_info)
        if _is_json_output(args):
            with contextlib.redirect_stdout(sys.stderr):
                result = client.upload_dataset(source, target, concurrency=args.concurrency)
            _print_json(asdict(result))
        else:
            result = client.upload_dataset(source, target, concurrency=args.concurrency)
            print(f"\nDone: {result.uploaded} uploaded, {result.skipped} skipped, {result.failed} failed")

        if result.failed > 0:
            sys.exit(1)

    async def _fs(self, args: argparse.Namespace) -> None:
        if args.fs_command == "ls":
            await self._fs_ls(args)
        elif args.fs_command == "get":
            await self._fs_get(args)
        elif args.fs_command == "download":
            await self._fs_download(args)
        else:
            raise ValueError(f"Unknown datasets fs command: {args.fs_command}")

    async def _fs_ls(self, args: argparse.Namespace) -> None:
        path = _normalize_task_path(args.path, allow_empty=True, directory=bool(args.path)) if args.path else ""
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)
        files = client.list_task_files(args.org, args.dataset, args.split, args.task, path.rstrip("/"))

        if _is_json_output(args):
            _print_json({
                "dataset": f"{args.org}/{args.dataset}",
                "split": args.split,
                "task": args.task,
                "path": path,
                "files": [asdict(f) for f in files],
            })
            return

        for file in files:
            print(file.path)

    async def _fs_get(self, args: argparse.Namespace) -> None:
        path = _normalize_task_path(args.path) if args.path else None
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)
        if path is None:
            files = client.list_task_files(args.org, args.dataset, args.split, args.task, "")
            if len(files) != 1:
                raise ValueError("--path is required when task contains zero or multiple files")
            path = files[0].path
        content = client.get_task_file(args.org, args.dataset, args.split, args.task, path)
        if content is None:
            raise FileNotFoundError(f"Task file not found: {args.org}/{args.dataset}/{args.split}/{args.task}/{path}")

        if _is_json_output(args):
            _print_json({
                "dataset": f"{args.org}/{args.dataset}",
                "split": args.split,
                "task": args.task,
                "path": path,
                "content": content.decode("utf-8"),
            })
            return

        _write_stdout_bytes(content)

    async def _fs_download(self, args: argparse.Namespace) -> None:
        path = _normalize_task_path(args.path, directory=args.path.endswith("/"))
        dest = Path(args.dest)
        registry_info = self._build_oss_registry_info(args)
        client = DatasetClient(registry_info)

        content = client.get_task_file(args.org, args.dataset, args.split, args.task, path)
        if content is not None:
            target = dest / Path(path).name if dest.is_dir() else dest
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if _is_json_output(args):
                _print_json({"downloaded": [str(target)]})
            else:
                print(str(target))
            return

        prefix = path if path.endswith("/") else f"{path}/"
        files = client.list_task_files(args.org, args.dataset, args.split, args.task, prefix.rstrip("/"))
        if not files:
            raise FileNotFoundError(f"Task path not found: {args.org}/{args.dataset}/{args.split}/{args.task}/{path}")

        downloaded: list[str] = []
        for file in files:
            file_content = client.get_task_file(args.org, args.dataset, args.split, args.task, file.path)
            if file_content is None:
                continue
            relative = file.path[len(prefix) :] if file.path.startswith(prefix) else file.path
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file_content)
            downloaded.append(str(target))

        if _is_json_output(args):
            _print_json({"downloaded": downloaded})
        else:
            for path in downloaded:
                print(path)

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction) -> None:
        datasets_parser = subparsers.add_parser("datasets", description="Dataset operations on OSS")
        datasets_parser.set_defaults(output=None)
        datasets_subparsers = datasets_parser.add_subparsers(dest="datasets_command")

        def add_output_arg(parser: argparse.ArgumentParser) -> None:
            parser.add_argument(
                "-o",
                "--output",
                "--ouput",
                dest="output",
                choices=["json"],
                default=argparse.SUPPRESS,
                help="Output format. Supported: json",
            )

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

        add_output_arg(datasets_parser)

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
        add_output_arg(tasks_parser)
        tasks_parser.add_argument("--org", required=True, help="Organization name")
        tasks_parser.add_argument("--dataset", required=True, help="Dataset name")
        tasks_parser.add_argument("--split", default="test", help="Split name (default: test)")
        tasks_parser.add_argument("--filter", default=None, help="Filter tasks by prefix (e.g. --filter 0xerr0r)")
        tasks_parser.add_argument("--offset", type=_non_negative_int, default=0, help="Skip first N tasks")
        tasks_parser.add_argument("--limit", type=_positive_int, default=None, help="Maximum number of tasks to show")
        add_oss_args(tasks_parser)

        splits_parser = datasets_subparsers.add_parser("splits", help="List splits under one dataset")
        splits_parser.add_argument("--org", required=True, help="Organization name")
        splits_parser.add_argument("--dataset", required=True, help="Dataset name")
        add_oss_args(splits_parser)

        upload_parser = datasets_subparsers.add_parser("upload", help="Upload local task dirs to OSS")
        add_output_arg(upload_parser)
        upload_parser.add_argument("--org", required=True, help="Organization name")
        upload_parser.add_argument("--dataset", required=True, help="Dataset name")
        upload_parser.add_argument("--split", required=True, help="Split name (e.g. train, test, v1.0)")
        upload_parser.add_argument(
            "--dir",
            required=True,
            help="Local dataset directory containing {task_id}/ subdirectories or direct task files, or one task file",
        )
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

        fs_parser = datasets_subparsers.add_parser("fs", aliases=["files"], help="Inspect files under one task")
        fs_subparsers = fs_parser.add_subparsers(dest="fs_command")

        def add_task_fs_args(parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--org", required=True, help="Organization name")
            parser.add_argument("--dataset", required=True, help="Dataset name")
            parser.add_argument("--split", default="test", help="Split name (default: test)")
            parser.add_argument("--task", required=True, help="Task ID")
            add_oss_args(parser)

        ls_parser = fs_subparsers.add_parser("ls", help="List files under one task")
        add_output_arg(ls_parser)
        add_task_fs_args(ls_parser)
        ls_parser.add_argument("--path", default="", help="Relative task directory path to list")

        get_parser = fs_subparsers.add_parser("get", help="Print one task file to stdout")
        add_output_arg(get_parser)
        add_task_fs_args(get_parser)
        get_parser.add_argument("--path", help="Relative task file path")

        download_parser = fs_subparsers.add_parser("download", help="Download one task file or directory")
        add_output_arg(download_parser)
        add_task_fs_args(download_parser)
        download_parser.add_argument("--path", required=True, help="Relative task file or directory path")
        download_parser.add_argument("--dest", required=True, help="Local destination file or directory")
