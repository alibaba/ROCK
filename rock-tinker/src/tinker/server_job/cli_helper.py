"""Command-line bridge for ServerJob lifecycle operations.

This module is intentionally owned by the Python SDK so external CLIs can call
ServerJob APIs without duplicating provider details.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from tinker.server_job.server_job import ServerJob


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, default=_json_default))


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    job = await ServerJob.attach(args.job_id)
    if args.command == "status":
        return (await job.status()).model_dump(mode="json")
    if args.command == "stop":
        return (await job.stop(force_after_seconds=args.force_after)).model_dump(mode="json")
    if args.command == "download-logs":
        return (await job.download_logs(args.output)).model_dump(mode="json")
    if args.command == "resolve-log":
        log_path = await job.resolve_log_path(kind=args.kind, runtime_id=args.runtime_id)
        return {
            "job_id": args.job_id,
            "kind": args.kind,
            "runtime_id": args.runtime_id,
            "log_path": str(log_path),
        }
    raise ValueError(f"Unknown command: {args.command}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tinker ServerJob CLI helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--job-id", required=True)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--job-id", required=True)
    stop.add_argument("--force-after", type=float, default=None)

    download = subparsers.add_parser("download-logs")
    download.add_argument("--job-id", required=True)
    download.add_argument("--output", default=None)

    resolve = subparsers.add_parser("resolve-log")
    resolve.add_argument("--job-id", required=True)
    resolve.add_argument("--kind", choices=["cookbook", "backend", "runtime"], default="cookbook")
    resolve.add_argument("--runtime-id", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _print_json(asyncio.run(_run(args)))
        return 0
    except Exception as exc:  # noqa: BLE001 - helper errors must be serialized for callers.
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
