"""Stable marker output for rockcli rl ServerJob discovery."""

from __future__ import annotations

import json
from typing import Any


def emit_server_job_marker(job: Any) -> None:
    payload = {
        "job_id": job.job_id,
        "platform": job.platform,
        "provider_job_id": job.provider_job_id,
        "base_url": job.base_url,
        "log_dir": job.log_dir,
    }
    print("TINKER_SERVER_JOB " + json.dumps(payload, ensure_ascii=False), flush=True)
