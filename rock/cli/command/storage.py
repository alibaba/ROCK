"""`rock storage get <sandbox_id>` — download an archived sandbox log tarball.

Thin CLI wrapper around :class:`rock.sdk.sandbox.storage_client.StorageClient`.
This file only handles argparse + user-facing output; all OSS/STS logic lives
in the SDK so it can be reused programmatically.
"""

import argparse
import os

from rock.cli.command.command import Command
from rock.logger import init_logger
from rock.sdk.sandbox.storage_client import ArchiveNotFoundError, StorageClient

logger = init_logger("rock.cli.storage")


class StorageCommand(Command):
    """rock storage get <sandbox_id> [-o PATH]"""

    name = "storage"

    async def arun(self, args: argparse.Namespace):
        action = getattr(args, "storage_action", None)
        if action == "get":
            await self._get(args)
            return
        # argparse with required=True surfaces missing-action errors itself,
        # but guard explicitly so subclasses/tests get a clear message.
        raise ValueError(f"Unknown storage action: {action!r}")

    async def _get(self, args: argparse.Namespace):
        client = StorageClient(
            base_url=args.base_url,
            auth_token=getattr(args, "auth_token", None),
            extra_headers=getattr(args, "extra_headers", None),
        )
        out_path = self._resolve_output_path(args.output, args.sandbox_id)

        try:
            written = await client.download_archived_log(
                sandbox_id=args.sandbox_id,
                output_path=out_path,
                archive_prefix=args.archive_prefix or "",
                bucket=args.bucket,
                endpoint=args.endpoint,
            )
        except ArchiveNotFoundError as e:
            print(f"NOT FOUND: {e}")
            logger.error(f"Archive not found for sandbox {args.sandbox_id}: {e}")
            return
        except RuntimeError:
            # Pre-flight errors (STS fetch, OSS config missing) — let them
            # propagate so the user sees the configuration explanation.
            raise
        except Exception as e:
            # OSS connectivity / unexpected oss2 errors — print + swallow.
            print(f"FAILED: {e}")
            logger.exception(f"Failed to download archive for {args.sandbox_id}: {e}")
            return

        print(f"OK: {written}")
        print(f"To extract: tar -xzf {written}")

    @staticmethod
    def _resolve_output_path(output: str | None, sandbox_id: str) -> str:
        if not output:
            return f"./{sandbox_id}.tar.gz"
        # If caller passed a directory (existing or with trailing /), drop the file inside.
        if output.endswith("/") or os.path.isdir(output):
            return os.path.join(output.rstrip("/"), f"{sandbox_id}.tar.gz")
        return output

    @staticmethod
    async def add_parser_to(subparsers: argparse._SubParsersAction):
        storage = subparsers.add_parser(
            "storage",
            help="Manage sandbox archive storage on OSS",
            description="Download archived sandbox log tarballs from OSS.",
        )
        storage_sub = storage.add_subparsers(dest="storage_action", required=True)

        get_p = storage_sub.add_parser("get", help="Download an archived sandbox log tarball")
        get_p.add_argument("sandbox_id", help="Sandbox id (matches the directory name under ROCK_LOGGING_PATH)")
        get_p.add_argument(
            "-o",
            "--output",
            default=None,
            help="Output file path or directory (default: ./<sandbox_id>.tar.gz)",
        )
        get_p.add_argument(
            "--archive-prefix",
            dest="archive_prefix",
            default="",
            help=(
                "OSS key prefix used at archive time (must match admin's "
                "sandbox_config.log.archive_prefix, e.g. 'rock-archives/')."
            ),
        )
        get_p.add_argument(
            "--bucket",
            default=None,
            help="Override the OSS bucket returned by admin /get_token",
        )
        get_p.add_argument(
            "--endpoint",
            default=None,
            help="Override the OSS endpoint returned by admin /get_token",
        )
