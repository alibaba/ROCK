import argparse

from rock.cli.command.command import Command
from rock.logger import init_logger
from rock.utils.oss_archiver import OssArchiver

logger = init_logger("rock.cli.storage")


class StorageCommand(Command):
    """Sandbox archive storage CLI.

    Currently supports:
        rock storage get <sandbox_id> [--out <dir>]

    Recovers the tar.gz archive previously uploaded by
    DockerDeployment._stop() under the ARCHIVE_THEN_CLEAN policy.
    Does NOT auto-extract — the operator decides.
    """

    name = "storage"

    def __init__(self):
        super().__init__()

    async def arun(self, args: argparse.Namespace):
        if not args.storage_action:
            raise ValueError("storage action is required (currently only 'get')")
        if args.storage_action == "get":
            await self._get(args)
        else:
            raise ValueError(f"Unknown storage action '{args.storage_action}'")

    async def _get(self, args: argparse.Namespace):
        oss_key = OssArchiver.build_sandbox_log_key(args.sandbox_id)
        out_path = f"{args.out.rstrip('/')}/{args.sandbox_id}.tar.gz"
        ok = await OssArchiver.get_object(oss_key, out_path)
        if ok:
            print(f"OK: {out_path}")
            print(f"To extract: tar -xzf {out_path}")
        else:
            print("FAILED")
            logger.error(
                f"Failed to download oss key {oss_key}. Check OssConfig and that the sandbox log was actually archived."
            )

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        storage_parser = subparsers.add_parser(
            "storage",
            description="Sandbox archive storage operations",
            help="Manage sandbox archive storage on OSS",
        )
        storage_subparsers = storage_parser.add_subparsers(
            dest="storage_action",
            help="Storage actions",
        )

        get_parser = storage_subparsers.add_parser(
            "get",
            help="Download an archived sandbox log tarball from OSS",
        )
        get_parser.add_argument(
            "sandbox_id",
            help="Sandbox container_name (matches the directory name under ${ROCK_LOGGING_PATH})",
        )
        get_parser.add_argument(
            "--out",
            default=".",
            help="Local output directory (default: current dir)",
        )
