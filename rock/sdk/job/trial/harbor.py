"""HarborTrial — execute a Harbor benchmark job inside a sandbox.

Extracted from rock.sdk.bench.job.Job. Combines dockerd startup and
``harbor jobs start -c`` into a single bash script executed by the
JobExecutor via the sandbox nohup protocol.
"""

from __future__ import annotations

import json

from rock.actions import Command, ReadFileRequest
from rock.logger import init_logger
from rock.sdk.bench.constants import USER_DEFINED_LOGS
from rock.sdk.bench.models.job.config import HarborJobConfig
from rock.sdk.bench.models.trial.result import HarborTrialResult
from rock.sdk.job.result import ExceptionInfo, TrialResult
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import register_trial

logger = init_logger(__name__)

_HARBOR_SCRIPT_TEMPLATE = r"""#!/bin/bash

# ── Detect and start dockerd ─────────────────────────────────────────
if command -v docker &>/dev/null; then
    echo "docker OK: $(command -v docker)"
    if ! pgrep -x dockerd &>/dev/null; then
        echo "Starting dockerd..."
        nohup dockerd &>/var/log/dockerd.log &
    fi
    for i in $(seq 1 60); do
        if docker info &>/dev/null; then echo "dockerd is ready"; break; fi
        sleep 1
        if [ "$i" -eq 60 ]; then echo "WARN: dockerd failed to start within 60s"; fi
    done
fi

# ── Ensure output directory exists ──────────────────────────────────
mkdir -p {user_defined_dir}

{meta_block}

# ── Harbor run ───────────────────────────────────────────────────────
set +e
harbor jobs start -c {config_path}
_rock_harbor_rc=$?
set -e

{meta_epilogue_block}

exit $_rock_harbor_rc
"""


class HarborTrial(AbstractTrial):
    """Harbor benchmark trial execution."""

    _config: HarborJobConfig

    async def setup(self, sandbox) -> None:
        await super().setup(sandbox)
        self._inject_runtime_labels(sandbox)
        # Write Harbor YAML config to sandbox
        yaml_content = self._config.to_harbor_yaml()
        config_path = f"{USER_DEFINED_LOGS}/rock_job_{self._config.job_name}.yaml"
        await sandbox.write_file_by_path(yaml_content, config_path)

    def _inject_runtime_labels(self, sandbox) -> None:
        labels = self._config.labels
        if self._config.environment.image:
            labels.setdefault("rock_sandbox_image", self._config.environment.image)
        if sandbox.sandbox_id:
            labels.setdefault("rock_sandbox_id", sandbox.sandbox_id)

    def _oss_mirror_enabled(self) -> bool:
        mirror = getattr(self._config.environment, "oss_mirror", None)
        return mirror is not None and mirror.enabled

    def build(self) -> str:
        config_path = f"{USER_DEFINED_LOGS}/rock_job_{self._config.job_name}.yaml"
        meta_dir = f"{self._config.jobs_dir}/{self._config.job_name}"

        if self._oss_mirror_enabled():
            meta_block, meta_epilogue_block = self._build_meta_blocks(meta_dir)
        else:
            meta_block = ""
            meta_epilogue_block = ""

        return _HARBOR_SCRIPT_TEMPLATE.format(
            config_path=config_path,
            user_defined_dir=USER_DEFINED_LOGS,
            meta_block=meta_block,
            meta_epilogue_block=meta_epilogue_block,
        )

    def _build_meta_blocks(self, meta_dir: str) -> tuple[str, str]:
        import os

        from rock.sdk.job.meta import render_meta_json

        meta_running = render_meta_json(self._config, job_type="harbor", status="running")

        user_id = getattr(self._config.environment, "user_id", None) or os.environ.get("ROCK_USER_ID")
        image = getattr(self._config.environment, "image", None)

        prologue = (
            f"mkdir -p {meta_dir}\n"
            "_rock_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
            f"cat > \"{meta_dir}/rock_meta.json\" << '__ROCK_META_EOF__'\n"
            f"{meta_running}\n"
            "__ROCK_META_EOF__\n"
        )

        epilogue = (
            "_rock_finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
            '_rock_status=$( [ $_rock_harbor_rc -eq 0 ] && echo "completed" || echo "failed" )\n'
            f'cat > "{meta_dir}/rock_meta.json" << __ROCK_META_EOF__\n'
            "{\n"
            '  "schema_version": "1",\n'
            f'  "job_name": "{self._config.job_name or ""}",\n'
            '  "job_type": "harbor",\n'
            '  "status": "$_rock_status",\n'
            f'  "namespace": {json.dumps(self._config.namespace)},\n'
            f'  "experiment_id": {json.dumps(self._config.experiment_id)},\n'
            f'  "user_id": {json.dumps(user_id)},\n'
            f'  "image": {json.dumps(image)},\n'
            f'  "labels": {json.dumps(self._config.labels)},\n'
            '  "started_at": "$_rock_started_at",\n'
            '  "finished_at": "$_rock_finished_at",\n'
            '  "exit_code": $_rock_harbor_rc\n'
            "}\n"
            "__ROCK_META_EOF__\n"
        )

        return prologue, epilogue

    async def collect(self, sandbox, output: str, exit_code: int) -> list[TrialResult]:
        """Return all Harbor sub-trial results (one entry per ``result.json``).

        Harbor writes N trial-level ``result.json`` files per sandbox run
        (one per dataset × task). We return them all so the Job layer can
        surface every sub-trial in ``JobResult.trial_results``. If Harbor
        crashed before any trial finished, return a single synthetic failure
        entry so that the caller can tell something ran.
        """
        trial_results = await self._collect_trial_results(sandbox)
        if trial_results:
            return list(trial_results)

        return [
            TrialResult(
                task_name=self._config.job_name or "",
                exception_info=ExceptionInfo(
                    exception_type="HarborNoTrials",
                    exception_message="No trial results found",
                ),
            )
        ]

    async def _collect_trial_results(self, sandbox) -> list[HarborTrialResult]:
        """Read trial-level result.json files from sandbox."""
        job_dir = f"{self._config.jobs_dir}/{self._config.job_name}"
        try:
            list_result = await sandbox.execute(
                Command(command=["find", job_dir, "-mindepth", "2", "-maxdepth", "2", "-name", "result.json"])
            )
            trial_files = [line.strip() for line in (list_result.stdout or "").strip().split("\n") if line.strip()]
        except Exception:
            trial_files = []

        results: list[HarborTrialResult] = []
        for trial_file in trial_files:
            try:
                response = await sandbox.read_file(ReadFileRequest(path=trial_file))
                data = json.loads(response.content)
                results.append(HarborTrialResult.from_harbor_json(data))
            except Exception as e:
                logger.warning(f"Failed to parse trial result {trial_file}: {e}")

        return results


# Auto-register
register_trial(HarborJobConfig, HarborTrial)
