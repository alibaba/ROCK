import argparse
import logging

from rock.cli.command.command import Command
from rock.sdk.builder.image_mirror import ImageMirror

logger = logging.getLogger(__name__)


class ImageCommand(Command):
    """Image command"""

    name = "image"

    def __init__(self):
        super().__init__()

    async def arun(self, args: argparse.Namespace):
        if "mirror" == args.image_command:
            await self.mirror(args)
        else:
            raise Exception(f"Unknown image command: {args.image_command}")

    async def mirror(self, args: argparse.Namespace):
        """Image mirror"""
        file_path = args.file
        mode = args.mode
        concurrency = args.concurrency
        source_registry = args.source_registry
        source_username = args.source_username
        source_password = args.source_password
        target_registry = args.target_registry
        target_username = args.target_username
        target_password = args.target_password
        auth_token = args.auth_token
        cluster = args.cluster

        image_mirror = ImageMirror()
        if "local" == mode:
            await image_mirror.build_batch(
                dataset=file_path,
                source_registry=source_registry,
                source_username=source_username,
                source_password=source_password,
                target_registry=target_registry,
                target_username=target_username,
                target_password=target_password,
            )
        else:
            await image_mirror.build_remote(
                authorization=auth_token,
                cluster=cluster,
                dataset=file_path,
                concurrency=concurrency,
                source_registry=source_registry,
                source_username=source_username,
                source_password=source_password,
                target_registry=target_registry,
                target_username=target_username,
                target_password=target_password,
            )

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        """Add command line parser"""
        image_parser = subparsers.add_parser(
            "image",
            description="Image operations",
        )
        image_subparsers = image_parser.add_subparsers(dest="image_command")

        # rock image mirror
        mirror_parser = image_subparsers.add_parser("mirror", help="Mirror image")
        mirror_parser.add_argument(
            "-f",
            "--file",
            required=True,
            help="jsonl, source file path. each line is a swebench-like instance",
        )
        mirror_parser.add_argument(
            "--mode",
            required=False,
            default="local",
            help="mirror mode, local or remote(default local)",
        )
        mirror_parser.add_argument(
            "--concurrency",
            type=int,
            default=3,
            choices=range(1, 51),
            help="number of concurrent mirrors (default: 3)",
        )
        mirror_parser.add_argument("--source-registry", required=False, help="source registry url")
        mirror_parser.add_argument("--source-username", required=False, help="source hub username")
        mirror_parser.add_argument("--source-password", required=False, help="source hub password")
        mirror_parser.add_argument("--target-registry", required=True, help="target registry url")
        mirror_parser.add_argument("--target-username", required=True, help="target hub username")
        mirror_parser.add_argument("--target-password", required=True, help="target hub password")
