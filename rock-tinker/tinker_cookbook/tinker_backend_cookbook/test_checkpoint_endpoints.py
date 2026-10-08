#!/usr/bin/env python3
"""Smoke test for delete_checkpoint and set_checkpoint_ttl endpoints.

Prereqs:
  Attach to a running local ServerJob that already has one or more checkpoints.
  Run train_swe_bench.py first; it creates a SAMPLER checkpoint via
  save_weights_for_sampler at the end of the train cycle.

What it does:
  1. GET  /api/v1/training_runs                        -> pick model_id
  2. GET  /api/v1/training_runs/<id>/checkpoints       -> pick checkpoint_id
  3. PUT  .../ttl  body={"ttl_seconds": 60}            -> expect 204
  4. PUT  .../ttl  body={"ttl_seconds": null}          -> expect 204 (clear)
  5. DELETE  .../checkpoints/<cid>                     -> expect 204
  6. GET checkpoints                                   -> assert <cid> gone
  7. DELETE again                                      -> expect 404
  8. PUT ttl on a non-existent checkpoint              -> expect 404

Usage:
  python tinker_cookbook/tinker_backend_cookbook/test_checkpoint_endpoints.py \
    --job-id local_YYYYMMDDTHHMMSS_xxxxxxxx \
    [--model-id <id>] \
    [--checkpoint-id <cid>]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

import tinker  # noqa: E402


async def _resolve_job_base_url(job_id: str) -> str:
    job = await tinker.ServerJob.attach(job_id)
    status = await job.status()
    if status.status != "running":
        raise RuntimeError(f"ServerJob {job_id} is {status.status}, expected running")
    if not job.base_url:
        raise RuntimeError(f"ServerJob {job_id} does not have a backend base_url")
    return job.base_url.rstrip("/")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--job-id",
        default=os.environ.get("TINKER_JOB_ID"),
        help="Running local ServerJob id; defaults to TINKER_JOB_ID.",
    )
    parser.add_argument(
        "--model-id",
        default=None,
        help="Specific model_id to test against; defaults to the first one returned",
    )
    parser.add_argument(
        "--checkpoint-id",
        default=None,
        help="Specific checkpoint_id; defaults to the first one for the model",
    )
    args = parser.parse_args()

    if not args.job_id:
        sys.exit("--job-id is required unless TINKER_JOB_ID is set")

    base = asyncio.run(_resolve_job_base_url(args.job_id))
    client = httpx.Client(base_url=base, timeout=30)

    # 1. List training runs.
    r = client.get("/api/v1/training_runs")
    r.raise_for_status()
    runs = r.json().get("training_runs", [])
    if not runs:
        sys.exit("No training runs on backend; run train_swe_bench.py first.")

    model_id = args.model_id or runs[0]["training_run_id"]
    print(f"[1/8] model_id = {model_id}  (out of {len(runs)} runs)")

    # 2. List checkpoints.
    r = client.get(f"/api/v1/training_runs/{model_id}/checkpoints")
    r.raise_for_status()
    ckpts = r.json().get("checkpoints", [])
    if not ckpts:
        sys.exit(
            f"No checkpoints for model {model_id}; "
            "run train_swe_bench.py first to create one."
        )

    ckpt_id = args.checkpoint_id or ckpts[0]["checkpoint_id"]
    ckpt_type = next(
        (c["checkpoint_type"] for c in ckpts if c["checkpoint_id"] == ckpt_id),
        ckpts[0]["checkpoint_type"],
    )
    print(
        f"[2/8] checkpoint_id = {ckpt_id}  (type={ckpt_type}; "
        f"{len(ckpts)} checkpoint(s) total before delete)"
    )

    ttl_url = f"/api/v1/training_runs/{model_id}/checkpoints/{ckpt_id}/ttl"
    del_url = f"/api/v1/training_runs/{model_id}/checkpoints/{ckpt_id}"

    # 3. PUT TTL = 60 seconds.
    r = client.put(ttl_url, json={"ttl_seconds": 60})
    if r.status_code != 204:
        sys.exit(f"set_ttl(60) expected 204, got {r.status_code}: {r.text}")
    print("[3/8] PUT ttl=60 -> 204")

    # 4. PUT TTL = None (clear).
    r = client.put(ttl_url, json={"ttl_seconds": None})
    if r.status_code != 204:
        sys.exit(f"set_ttl(None) expected 204, got {r.status_code}: {r.text}")
    print("[4/8] PUT ttl=null -> 204")

    # 5. DELETE.
    r = client.delete(del_url)
    if r.status_code != 204:
        sys.exit(f"DELETE expected 204, got {r.status_code}: {r.text}")
    print("[5/8] DELETE -> 204")

    # 6. Verify gone.
    r = client.get(f"/api/v1/training_runs/{model_id}/checkpoints")
    r.raise_for_status()
    remaining = [c["checkpoint_id"] for c in r.json().get("checkpoints", [])]
    if ckpt_id in remaining:
        sys.exit(
            f"checkpoint {ckpt_id} still listed after delete; "
            f"remaining={remaining}"
        )
    print(f"[6/8] verified gone (remaining: {remaining})")

    # 7. DELETE again -> 404.
    r = client.delete(del_url)
    if r.status_code != 404:
        sys.exit(f"second DELETE expected 404, got {r.status_code}: {r.text}")
    print("[7/8] second DELETE -> 404")

    # 8. PUT TTL on the now-deleted checkpoint -> 404.
    r = client.put(ttl_url, json={"ttl_seconds": 60})
    if r.status_code != 404:
        sys.exit(
            f"PUT ttl on deleted ckpt expected 404, got {r.status_code}: {r.text}"
        )
    print("[8/8] PUT ttl on deleted ckpt -> 404")

    print("\nAll checkpoint endpoint tests passed.")


if __name__ == "__main__":
    main()
