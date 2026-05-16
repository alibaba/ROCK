"""`rock storage get <sandbox_id>` — download an archived sandbox log tarball.

The archival side (admin scheduler) ships stopped sandbox log dirs to OSS
under `<archive_prefix>sandbox-logs/<sandbox_id>.tar.gz`. This CLI is the
recovery path: ask admin for primary STS via `/get_token?account=primary`,
then download the object via oss2.

Design choices:
  - Reuses `build_sandbox_log_key` from rock.utils.archive_command so admin
    and CLI cannot drift on the OSS key layout.
  - AK/SK never leave admin → CLI is given a short-lived STS token.
  - No new admin endpoints; the existing /get_token covers it.
"""

import argparse
import asyncio
import os
from typing import Any

import oss2

from rock.cli.command.command import Command
from rock.logger import init_logger
from rock.utils.archive_command import build_sandbox_log_key
from rock.utils.http import HttpUtils

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
        sts = await self._fetch_primary_sts(args)
        bucket_name, endpoint, region = self._extract_oss_target(args, sts)
        oss_key = build_sandbox_log_key(args.sandbox_id, args.archive_prefix or "")
        out_path = self._resolve_output_path(args.output, args.sandbox_id)

        bucket = oss2.Bucket(
            auth=oss2.StsAuth(sts["AccessKeyId"], sts["AccessKeySecret"], sts["SecurityToken"]),
            endpoint=endpoint,
            bucket_name=bucket_name,
            region=region,
        )

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        try:
            await asyncio.to_thread(bucket.get_object_to_file, oss_key, out_path)
        except oss2.exceptions.NoSuchKey:
            print(f"NOT FOUND: oss://{bucket_name}/{oss_key}")
            logger.error(f"Archive not found for sandbox {args.sandbox_id} at {oss_key}")
            return
        except Exception as e:
            print(f"FAILED: {e}")
            logger.exception(f"Failed to download {oss_key}: {e}")
            return

        print(f"OK: {out_path}")
        print(f"To extract: tar -xzf {out_path}")

    async def _fetch_primary_sts(self, args: argparse.Namespace) -> dict[str, Any]:
        url = f"{args.base_url.rstrip('/')}/get_token?account=primary"
        headers = self._build_headers(args)
        response = await HttpUtils.get(url, headers)
        if response.get("status") != "Success":
            raise RuntimeError(f"admin /get_token returned: {response.get('message') or response}")
        result = response.get("result")
        if not result:
            raise RuntimeError("admin /get_token returned an empty result; check OssConfig.primary on admin")
        return result

    @staticmethod
    def _build_headers(args: argparse.Namespace) -> dict[str, str]:
        headers = dict(getattr(args, "extra_headers", {}) or {})
        token = getattr(args, "auth_token", None)
        if token:
            headers["xrl-authorization"] = token
        return headers

    @staticmethod
    def _extract_oss_target(args: argparse.Namespace, sts: dict[str, Any]) -> tuple[str, str, str | None]:
        # The /get_token response from a recent admin includes Endpoint/Bucket/Region
        # for the primary account. CLI flags override (useful when testing against
        # a non-default bucket without redeploying admin).
        bucket = args.bucket or sts.get("Bucket")
        endpoint = args.endpoint or sts.get("Endpoint")
        region = sts.get("Region")
        if not bucket or not endpoint:
            raise RuntimeError(
                "OSS bucket/endpoint missing — pass --bucket/--endpoint or configure "
                "OssConfig.primary.bucket/endpoint on admin"
            )
        return bucket, endpoint, region

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
            help="Override the OSS key prefix used at archive time (must match admin OssConfig.archive_prefix)",
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
