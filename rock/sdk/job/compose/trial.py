"""ComposeTrial — multi-container Docker Compose job inside a DinD sandbox (v2).

v2 delegates all container orchestration to ``docker compose up`` —
no more hand-written docker run phases.

setup()  → upload files (compose + scripts) via environment.uploads; render minimal runner.sh
build()  → "bash /rock/runner.sh"
collect()→ exit code = main service exit code (--exit-code-from main)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from rock.logger import init_logger
from rock.sdk.job.compose.config import ComposeJobConfig
from rock.sdk.job.result import ExceptionInfo, TrialResult
from rock.sdk.job.trial.abstract import AbstractTrial
from rock.sdk.job.trial.registry import register_trial

if TYPE_CHECKING:
    from rock.sdk.sandbox.client import Sandbox

logger = init_logger(__name__)

_OSS_CREDENTIAL_FIELDS = (
    "oss_access_key_id",
    "oss_access_key_secret",
    "oss_endpoint",
    "oss_region",
    "oss_bucket",
)

# ── runner.sh template (v2 minimal) ──────────────────────────────────────────
# Placeholders use __UPPER__ style to avoid collision with bash ${var} syntax.
# NEVER use str.format() on this template — it contains {} in bash constructs.

_RUNNER_TEMPLATE = r"""#!/bin/bash
# runner.sh — ROCK ComposeJob runtime (v2: delegates to docker compose)
set -uo pipefail
COMPOSE_FILE="/rock/compose/docker-compose.yaml"
LOG_DIR="/rock/logs"; mkdir -p "$LOG_DIR"
RUNNER_EXIT=0

cleanup_all() {
  docker compose -f "$COMPOSE_FILE" logs --no-color > "$LOG_DIR/compose.log" 2>&1 || true
  docker compose -f "$COMPOSE_FILE" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup_all EXIT
trap 'RUNNER_EXIT=143; exit 143' TERM INT

# ── P0: bootstrap dockerd and wait for it to be ready ──
# NOTE: In a ROCK kata DinD sandbox, dockerd is NOT running on entry. We must
# start it ourselves. Two kata-environment gotchas learned from real runs:
#   1. nohup'd dockerd does not inherit the interactive shell PATH, so it fails
#      with "containerd executable file not found" — we export PATH explicitly.
#   2. the kata guest lacks /proc/sys/net/bridge/bridge-nf-call-iptables, so
#      dockerd's default bridge network init fails unless we set
#      DOCKER_IGNORE_BR_NETFILTER_ERROR=1.
echo "[runner] P0: wait docker daemon"
if ! docker info >/dev/null 2>&1; then
  if ! pgrep -x dockerd >/dev/null 2>&1; then
    PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin \
    DOCKER_IGNORE_BR_NETFILTER_ERROR=1 nohup dockerd >/var/log/dockerd.log 2>&1 &
  fi
fi
for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
docker info >/dev/null 2>&1 || { echo "docker daemon not ready after 60s"; exit 1; }

# Optional: private registry login (credentials from SandboxConfig.registry_*)
__REGISTRY_LOGIN__

# ── P1: docker compose up — main service exit code drives the overall result ──
echo "[runner] P1: docker compose up"
docker compose -f "$COMPOSE_FILE" up \
  __ABORT_FLAG__ \
  --exit-code-from main 2>&1 | tee "$LOG_DIR/up.log"
RUNNER_EXIT=${PIPESTATUS[0]}
echo "[runner] main service exited rc=$RUNNER_EXIT"

# ── P2: optional OSS artifact upload (only when environment.oss_mirror.enabled) ──
__PHASE2_OSS_UPLOAD__

exit "$RUNNER_EXIT"
"""


# ── render helpers ────────────────────────────────────────────────────────────


def _render_registry_login(config: ComposeJobConfig) -> str:
    """Render optional docker login block (conditional at runtime on $REGISTRY_USERNAME)."""
    return (
        'if [ -n "${REGISTRY_USERNAME:-}" ]; then\n'
        '  docker login "${REGISTRY_HOST:-}" -u "$REGISTRY_USERNAME" -p "$REGISTRY_PASSWORD" >/dev/null 2>&1 || true\n'
        "fi"
    )


def _render_oss_upload(config: ComposeJobConfig) -> str:
    """Render P2 OSS artifact upload block (empty comment when not enabled)."""
    mirror = config.environment.oss_mirror
    if mirror is None or not mirror.enabled:
        return "# (oss_mirror not enabled — skip upload)"
    return (
        'echo "[runner] P2: uploading artifacts to OSS ..."\n'
        'ossutil cp "$LOG_DIR/" "oss://$OSS_BUCKET/$ROCK_OSS_PREFIX/" \\\n'
        "    --recursive -f \\\n"
        '    || echo "[runner] oss upload failed (rc=$?), ignored" >&2'
    )


# ── ComposeTrial ──────────────────────────────────────────────────────────────


class ComposeTrial(AbstractTrial):
    """Docker Compose multi-container trial (v2).

    Uploads the compose file + scripts, renders a minimal runner.sh that
    bootstraps dockerd and delegates all orchestration to ``docker compose up``.

    setup()  → _upload_files() + render/write runner.sh
    build()  → "bash /rock/runner.sh"
    collect()→ exit_code != 0 → ExceptionInfo(ComposeMainServiceFailed); reads compose.log
    """

    _config: ComposeJobConfig

    def _oss_mirror_enabled(self) -> bool:
        mirror = self._config.environment.oss_mirror
        return mirror is not None and mirror.enabled

    def _prepare_oss_session_env(self) -> None:
        """Resolve OSS credentials and inject ROCK_* keys into environment.env.

        Resolution order (same as BashTrial):
          1. OssMirrorConfig field
          2. environment.env
          3. host os.environ
        """
        mirror = self._config.environment.oss_mirror
        env = self._config.environment.env

        for field_name in _OSS_CREDENTIAL_FIELDS:
            env_key = field_name.upper()
            v = getattr(mirror, field_name, None) or env.get(env_key) or os.environ.get(env_key)
            if v:
                env[env_key] = v

        if not self._config.namespace:
            raise ValueError("oss_mirror: namespace is not set (sandbox did not return one)")
        if not self._config.experiment_id:
            raise ValueError("oss_mirror: experiment_id is not set (sandbox did not return one)")
        for env_key in ("OSS_BUCKET", "OSS_ENDPOINT", "OSS_REGION"):
            if not env.get(env_key):
                raise ValueError(f"oss_mirror.enabled=True but {env_key} is not resolvable")

        from rock import env_vars

        env["ROCK_ARTIFACT_DIR"] = env_vars.ROCK_BASH_JOB_ARTIFACT_DIR
        env["ROCK_OSS_PREFIX"] = (
            f"artifacts/{self._config.namespace}/{self._config.experiment_id}/{self._config.job_name}"
        )

    async def on_sandbox_ready(self, sandbox: Sandbox) -> None:
        """Backfill namespace/experiment_id, then prepare OSS session env if needed."""
        await super().on_sandbox_ready(sandbox)
        if self._oss_mirror_enabled():
            self._prepare_oss_session_env()

    async def setup(self, sandbox: Sandbox) -> None:
        """Upload compose file + scripts; render and write minimal runner.sh.

        Deliberately does NOT call super().setup() to skip _setup_proxy —
        DinD compose jobs manage their own networking and proxy sidecar via
        docker-compose.yaml. We call _upload_files() directly.
        """
        await self._upload_files(sandbox)

        runner = self._render_runner_sh()
        await sandbox.write_file_by_path(runner, "/rock/runner.sh")
        await sandbox.arun("chmod +x /rock/runner.sh")

    def build(self) -> str:
        return "bash /rock/runner.sh"

    async def collect(self, sandbox: Sandbox, output: str, exit_code: int) -> TrialResult:
        """Collect result: on failure wrap exit code into ComposeMainServiceFailed.

        docker compose logs are captured by runner.sh's trap EXIT into
        /rock/logs/compose.log — read it here for diagnostics.
        """
        exc: ExceptionInfo | None = None
        if exit_code != 0:
            exc = ExceptionInfo(
                exception_type="ComposeMainServiceFailed",
                exception_message=f"main service exited with {exit_code}",
            )

        compose_log_obs = await sandbox.arun("cat /rock/logs/compose.log 2>/dev/null || true")
        if compose_log_obs.output:
            logger.info("[compose-trial] compose log:\n%s", compose_log_obs.output)

        return TrialResult(
            task_name=self._config.job_name or "",
            exception_info=exc,
            raw_output=output,
            exit_code=exit_code,
        )

    def _render_runner_sh(self) -> str:
        """Render runner.sh from the template using str.replace only.

        Uses __PLACEHOLDER__ tokens — never str.format() — to safely handle
        bash ${var}, ${PIPESTATUS[0]}, and {} literals inside the template.
        """
        runner = _RUNNER_TEMPLATE

        # P0: registry login (always rendered; conditional at runtime)
        runner = runner.replace("__REGISTRY_LOGIN__", _render_registry_login(self._config))

        # P1: abort flag
        abort_flag = "--abort-on-container-exit" if self._config.abort_on_container_exit else ""
        runner = runner.replace("__ABORT_FLAG__", abort_flag)

        # P2: OSS upload
        runner = runner.replace("__PHASE2_OSS_UPLOAD__", _render_oss_upload(self._config))

        return runner


# Auto-register on import
register_trial(ComposeJobConfig, ComposeTrial)
