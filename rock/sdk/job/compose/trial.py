"""ComposeTrial — Docker Compose multi-container job trial inside a DinD sandbox.

The outer sandbox is a Docker-in-Docker (DinD) environment. This trial
generates a ``runner.sh`` that orchestrates inner containers (init → sidecars →
main) entirely through the ``docker`` CLI available in the outer sandbox.
"""

from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING

from rock.logger import init_logger
from rock.sdk.job.compose.config import (
    ComposeJobConfig,
    InitContainerSpec,
    MainContainerSpec,
    OssDep,
    ResourceSpec,
    SecretEnvEntry,
    SidecarSpec,
)
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

# ── runner.sh skeleton ────────────────────────────────────────────────────────
# Placeholders use __UPPER__ style to avoid collision with bash ${var} syntax.
# NEVER use str.format() on this template — it contains {} in bash constructs.

_RUNNER_SKELETON = r"""#!/bin/bash
set +e

RUNNER_EXIT=0

# ────────────────────────────────────────────────────────────────────────────
# cleanup_all — invoked by trap EXIT
# ────────────────────────────────────────────────────────────────────────────
cleanup_all() {
    echo "[rock-compose] Cleaning up containers / network / volume ..."
    docker rm -f rock-main-$$ 2>/dev/null || true
__SIDECAR_CLEANUP__
    docker network rm rock_compose_$$ 2>/dev/null || true
    docker volume rm rock_shared_$$ 2>/dev/null || true
}

trap cleanup_all EXIT

# ────────────────────────────────────────────────────────────────────────────
# P0 — start dockerd (ROCK kata sandbox does NOT auto-start it), then wait ready
# ────────────────────────────────────────────────────────────────────────────
# NOTE: in a ROCK kata DinD sandbox dockerd is NOT running on entry. We must
# start it ourselves. Gotchas learned from real runs on the kata backend:
#   1. nohup'd dockerd does not inherit the interactive shell PATH, so it fails
#      with "containerd executable file not found" — we export PATH explicitly.
#   2. the kata guest lacks /proc/sys/net/bridge/bridge-nf-call-iptables, so
#      dockerd's default bridge network init fails unless we set
#      DOCKER_IGNORE_BR_NETFILTER_ERROR=1.
echo "[rock-compose] P0: starting dockerd ..."
if ! docker info >/dev/null 2>&1; then
    if ! pgrep -x dockerd >/dev/null 2>&1; then
        PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin \
        DOCKER_IGNORE_BR_NETFILTER_ERROR=1 \
            nohup dockerd >/var/log/dockerd.log 2>&1 &
    fi
fi
for i in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
        echo "[rock-compose] dockerd is ready"
        break
    fi
    sleep 2
    if [ "$i" -eq 60 ]; then
        echo "[rock-compose] ERROR: dockerd not ready after 120s"
        echo "[rock-compose] --- dockerd.log tail ---"
        tail -20 /var/log/dockerd.log 2>/dev/null || true
        exit 1
    fi
done

docker network create rock_compose_$$ 2>/dev/null || true
docker volume create rock_shared_$$ 2>/dev/null || true

__REGISTRY_LOGIN__

mkdir -p /rock/logs

# ────────────────────────────────────────────────────────────────────────────
# P1 — OSS dependency download (conditional)
# ────────────────────────────────────────────────────────────────────────────
__OSS_DEPS__

# ────────────────────────────────────────────────────────────────────────────
# P2 — init containers (serial)
# ────────────────────────────────────────────────────────────────────────────
echo "[rock-compose] P2: starting init containers ..."
__INIT_CONTAINERS__

# ────────────────────────────────────────────────────────────────────────────
# P3 — sidecar containers (parallel, detached)
# ────────────────────────────────────────────────────────────────────────────
echo "[rock-compose] P3: starting sidecar containers ..."
__SIDECAR_CONTAINERS__

# ────────────────────────────────────────────────────────────────────────────
# P4 — health probes for sidecars that declare health
# ────────────────────────────────────────────────────────────────────────────
echo "[rock-compose] P4: health probes ..."
__HEALTH_PROBES__

# ────────────────────────────────────────────────────────────────────────────
# P5 — main container (foreground)
# ────────────────────────────────────────────────────────────────────────────
echo "[rock-compose] P5: starting main container ..."
__MAIN_CONTAINER__
RUNNER_EXIT=${PIPESTATUS[0]}
echo "[rock-compose] main exited with code $RUNNER_EXIT"

# ────────────────────────────────────────────────────────────────────────────
# P6 — collect sidecar logs + optional OSS mirror upload
# ────────────────────────────────────────────────────────────────────────────
echo "[rock-compose] P6: collecting logs ..."
__COLLECT_SIDECAR_LOGS__
__OSS_MIRROR_UPLOAD__

# ────────────────────────────────────────────────────────────────────────────
# P7 — explicit exit with main's exit code (cleanup via trap)
# ────────────────────────────────────────────────────────────────────────────
exit $RUNNER_EXIT
"""


# ── helpers ───────────────────────────────────────────────────────────────────


def _resource_args(res: ResourceSpec | None) -> list[str]:
    """Convert ResourceSpec to docker run resource flag strings."""
    args: list[str] = []
    if res is None:
        return args
    cpu = res.cpu_limit if res.cpu_limit is not None else res.cpus
    if cpu is not None:
        args.append(f"--cpus {cpu}")
    if res.memory is not None:
        args.append(f"--memory-reservation {shlex.quote(res.memory)}")
    mem_limit = res.memory_limit if res.memory_limit is not None else None
    if mem_limit is not None:
        args.append(f"--memory {shlex.quote(mem_limit)}")
    return args


def _volume_args(volume_mounts) -> list[str]:
    """Render -v flags for volume_mounts.

    host_path set → bind-mount real outer path; otherwise use the shared named volume.
    """
    args: list[str] = []
    for vm in volume_mounts:
        suffix = ":ro" if vm.read_only else ""
        if vm.host_path:
            args.append(f"-v {shlex.quote(vm.host_path)}:{shlex.quote(vm.mount_path)}{suffix}")
        else:
            args.append(f"-v rock_shared_$$:{shlex.quote(vm.mount_path)}{suffix}")
    return args


def _env_args(env: dict[str, str], secret_env: dict[str, SecretEnvEntry]) -> list[str]:
    """Build -e flags for plain env and secret_env (shell variable references)."""
    args: list[str] = []
    for k, v in env.items():
        args.append(f"-e {shlex.quote(k)}={shlex.quote(v)}")
    for k in secret_env:
        # Render secret as a shell variable reference — value is never embedded literally.
        args.append(f'-e {shlex.quote(k)}="${{{k}}}"')
    return args


def _entrypoint_args(spec) -> tuple[list[str], str]:
    """Return (flag_args, positional_cmd_str) for a container spec.

    Returns:
        flag_args   — e.g. ["--entrypoint bash"] or []
        positional  — positional command string after the image, e.g. "bash /rock/scripts/name.sh"
    """
    flag_args: list[str] = []
    positional = ""

    if spec.command:
        # Override entrypoint with command[0]; remaining command args + spec.args → positional
        flag_args.append(f"--entrypoint {shlex.quote(spec.command[0])}")
        remainder = list(spec.command[1:]) + (spec.args or [])
        if remainder:
            positional = " ".join(shlex.quote(a) for a in remainder)
    elif spec.script_path:
        positional = f"bash {shlex.quote(spec.script_path)}"
    elif spec.script:
        # Inline script was written to /rock/scripts/<name>.sh during setup
        positional = f"bash /rock/scripts/{spec.name}.sh"
    # else: use image's own ENTRYPOINT/CMD — no flags or positional needed

    return flag_args, positional


def _render_oss_deps(oss_deps: list[OssDep]) -> str:
    """Render P1 OSS dependency download block."""
    if not oss_deps:
        return "# (no oss_deps)"
    lines = ['echo "[rock-compose] P1: downloading OSS dependencies ..."']
    for dep in oss_deps:
        key_q = shlex.quote(dep.key)
        target_q = shlex.quote(dep.target_path)
        if dep.extract:
            lines.append(
                f"ossutil cp {key_q} /tmp/_rock_dep_archive && "
                f"mkdir -p {target_q} && "
                f"tar -xf /tmp/_rock_dep_archive -C {target_q}"
            )
        else:
            lines.append(f"ossutil cp {key_q} {target_q}")
    return "\n".join(lines)


def _render_init_containers(init_containers: list[InitContainerSpec]) -> str:
    """Render P2 init container serial execution block."""
    if not init_containers:
        return "# (no init containers)"
    lines = []
    for ic in init_containers:
        run_parts = [
            "docker run --rm",
            "--network rock_compose_$$",
            f"--network-alias {shlex.quote(ic.name)}",
            "-v rock_shared_$$:/rock/shared",
            "-v /rock/scripts:/rock/scripts:ro",
        ]
        run_parts.extend(_resource_args(ic.resources))
        if ic.privileged:
            run_parts.append("--privileged")
        run_parts.extend(_env_args(ic.env, ic.secret_env))
        run_parts.extend(_volume_args(ic.volume_mounts))
        flag_a, pos = _entrypoint_args(ic)
        run_parts.extend(flag_a)
        run_parts.append(shlex.quote(ic.image))
        if pos:
            run_parts.append(pos)

        cmd = " \\\n    ".join(run_parts)
        lines.append(f'echo "[rock-compose] init: {ic.name}"')
        lines.append(cmd)
        lines.append(f'if [ $? -ne 0 ]; then echo "[rock-compose] init container {ic.name} failed"; exit 1; fi')
    return "\n".join(lines)


def _render_sidecar_containers(sidecars: list[SidecarSpec]) -> str:
    """Render P3 sidecar container launch block (detached)."""
    if not sidecars:
        return "# (no sidecars)"
    lines = []
    for sc in sidecars:
        run_parts = [
            "docker run -d",
            f"--name rock-sidecar-{sc.name}-$$",
            "--network rock_compose_$$",
            f"--network-alias {shlex.quote(sc.name)}",
            "-v rock_shared_$$:/rock/shared",
            "-v /rock/scripts:/rock/scripts:ro",
        ]
        run_parts.extend(_resource_args(sc.resources))
        if sc.privileged:
            run_parts.append("--privileged")
        run_parts.extend(_env_args(sc.env, sc.secret_env))
        run_parts.extend(_volume_args(sc.volume_mounts))
        flag_a, pos = _entrypoint_args(sc)
        run_parts.extend(flag_a)
        run_parts.append(shlex.quote(sc.image))
        if pos:
            run_parts.append(pos)

        cmd = " \\\n    ".join(run_parts)
        lines.append(f'echo "[rock-compose] sidecar: {sc.name}"')
        lines.append(cmd)
    return "\n".join(lines)


def _render_health_probes(sidecars: list[SidecarSpec]) -> str:
    """Render P4 health probe block for sidecars that declare health."""
    health_sidecars = [sc for sc in sidecars if sc.health is not None]
    if not health_sidecars:
        return "# (no health probes)"
    lines = []
    for sc in health_sidecars:
        h = sc.health
        if h is None:
            continue  # type narrowing guard (already filtered above)
        timeout = h.timeout_sec
        port = h.port
        lines.append(f'echo "[rock-compose] health probe: {sc.name}:{port} (timeout {timeout}s)"')
        lines.append("_rock_health_ok=0")
        lines.append(f"for _i in $(seq 1 {timeout}); do")
        lines.append(
            f"    if docker run --rm --network rock_compose_$$ busybox "
            f"nc -z {shlex.quote(sc.name)} {port} 2>/dev/null; then"
        )
        lines.append(f'        echo "[rock-compose] sidecar {sc.name} is ready"; _rock_health_ok=1; break; fi')
        lines.append("    sleep 1")
        lines.append("done")
        lines.append(
            f'if [ "$_rock_health_ok" -eq 0 ]; then echo "[rock-compose] ERROR: {sc.name} not ready after {timeout}s"; exit 1; fi'
        )
    return "\n".join(lines)


def _render_main_container(main: MainContainerSpec) -> str:
    """Render P5 main container execution (foreground, tee logs)."""
    run_parts = [
        "docker run --name rock-main-$$",
        "--network rock_compose_$$",
        "--network-alias main",
        "-v rock_shared_$$:/rock/shared",
        # Mount the outer-sandbox scripts dir so the container can run main.sh.
        # /rock/scripts lives in the OUTER sandbox; inner containers need it bind-mounted.
        "-v /rock/scripts:/rock/scripts:ro",
    ]
    run_parts.extend(_resource_args(main.resources))
    if main.privileged:
        run_parts.append("--privileged")
    run_parts.extend(_env_args(main.env, main.secret_env))
    run_parts.extend(_volume_args(main.volume_mounts))
    run_parts.append(shlex.quote(main.image))
    # Main entrypoint is always bash /rock/scripts/main.sh (script/script_path uploaded there)
    run_parts.append("bash /rock/scripts/main.sh")

    cmd = " \\\n    ".join(run_parts)
    return f"{cmd} 2>&1 | tee /rock/logs/main.log"


def _render_collect_sidecar_logs(sidecars: list[SidecarSpec]) -> str:
    """Render P6 sidecar log collection and stop."""
    if not sidecars:
        return "# (no sidecars to collect)"
    lines = []
    for sc in sidecars:
        lines.append(f"docker logs rock-sidecar-{sc.name}-$$ > /rock/logs/{sc.name}.log 2>&1 || true")
        lines.append(f"docker stop rock-sidecar-{sc.name}-$$ 2>/dev/null || true")
    return "\n".join(lines)


def _render_sidecar_cleanup(sidecars: list[SidecarSpec]) -> str:
    """Render per-sidecar docker rm -f lines for cleanup_all."""
    if not sidecars:
        return ""
    lines = [f"    docker rm -f rock-sidecar-{sc.name}-$$ 2>/dev/null || true" for sc in sidecars]
    return "\n".join(lines)


def _render_oss_mirror_upload(config: ComposeJobConfig) -> str:
    """Render P6 OSS mirror upload block (conditional)."""
    mirror = config.environment.oss_mirror
    if mirror is None or not mirror.enabled:
        return "# (no oss mirror upload)"
    return (
        'echo "[rock-compose] uploading artifacts to OSS ..."\n'
        'ossutil cp /rock/logs/ "oss://$OSS_BUCKET/$ROCK_OSS_PREFIX/" \\\n'
        "    --recursive -f \\\n"
        '    || echo "[rock-compose] oss upload failed (rc=$?), ignored" >&2'
    )


def _render_registry_login(config: ComposeJobConfig) -> str:
    """Render optional docker login using registry credentials from env."""
    env = config.environment.env
    registry = env.get("ROCK_REGISTRY_HOST", "")
    if not registry:
        return "# (no registry login)"
    return (
        f'echo "[rock-compose] logging in to registry {registry} ..."\n'
        f"docker login {shlex.quote(registry)} \\\n"
        f'    -u "$ROCK_REGISTRY_USER" \\\n'
        f'    -p "$ROCK_REGISTRY_PASSWORD" || true'
    )


# ── ComposeTrial ──────────────────────────────────────────────────────────────


class ComposeTrial(AbstractTrial):
    """Docker Compose multi-container trial.

    Manages inner container orchestration inside a DinD outer sandbox via
    a generated ``runner.sh`` script.
    """

    _config: ComposeJobConfig

    def __init__(self, config: ComposeJobConfig):
        super().__init__(config)
        self._ossutil_ready: bool = False

    def _oss_mirror_enabled(self) -> bool:
        mirror = self._config.environment.oss_mirror
        return mirror is not None and mirror.enabled

    def _prepare_oss_session_env(self) -> None:
        """Resolve OSS credentials and inject ROCK_* keys into environment.env.

        Follows the same resolution order as BashTrial:
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
        """Backfill namespace/experiment_id then prepare OSS session env."""
        await super().on_sandbox_ready(sandbox)
        if self._oss_mirror_enabled():
            self._prepare_oss_session_env()

    async def setup(self, sandbox: Sandbox) -> None:
        """Upload files, write inline container scripts, and render runner.sh.

        Deliberately does NOT call super().setup() to skip _setup_proxy —
        DinD compose jobs manage their own networking and proxy sidecar.
        However, we call _upload_files() directly to handle environment.uploads.
        """
        # Upload user-specified files (skip _setup_proxy)
        await self._upload_files(sandbox)

        compose = self._config.compose

        # Write inline scripts for init/sidecar containers that have script= set
        all_containers: list = list(compose.init_containers) + list(compose.sidecars)
        for ctr in all_containers:
            if ctr.script:
                await sandbox.write_file_by_path(ctr.script, f"/rock/scripts/{ctr.name}.sh")

        # Write main container script (from top-level script / script_path)
        main_script = self._config.script or ""
        if self._config.script_path:
            from pathlib import Path

            main_script = Path(self._config.script_path).read_text()
        await sandbox.write_file_by_path(main_script, "/rock/scripts/main.sh")

        # Ensure ossutil available if any oss_deps declared
        if compose.main.oss_deps:
            self._ossutil_ready = await sandbox.fs.ensure_ossutil()
            if not self._ossutil_ready:
                logger.warning("ossutil install failed — OSS deps download may fail")

        # Render and write runner.sh
        runner_content = self._render_runner_sh()
        await sandbox.write_file_by_path(runner_content, "/rock/runner.sh")

    def build(self) -> str:
        return "bash /rock/runner.sh"

    async def collect(self, sandbox: Sandbox, output: str, exit_code: int) -> TrialResult:
        """Collect result: on failure, capture container logs for diagnostics."""
        exception_info = None
        if exit_code != 0:
            exception_info = ExceptionInfo(
                exception_type="ComposeMainContainerFailed",
                exception_message=f"Compose main container exited with code {exit_code}",
            )

        compose = self._config.compose

        # Collect main container log
        main_log_obs = await sandbox.arun("cat /rock/logs/main.log 2>/dev/null || true")
        if main_log_obs.output:
            logger.info("[rock-compose] main log:\n%s", main_log_obs.output)

        # Collect sidecar logs
        for sc in compose.sidecars:
            sc_log_obs = await sandbox.arun(f"cat /rock/logs/{sc.name}.log 2>/dev/null || true")
            if sc_log_obs.output:
                logger.info("[rock-compose] sidecar %s log:\n%s", sc.name, sc_log_obs.output)

        # Collect init container logs (best-effort, may not exist)
        for ic in compose.init_containers:
            ic_log_obs = await sandbox.arun(f"cat /rock/logs/{ic.name}.log 2>/dev/null || true")
            if ic_log_obs.output:
                logger.info("[rock-compose] init %s log:\n%s", ic.name, ic_log_obs.output)

        return TrialResult(
            task_name=self._config.job_name or "",
            exception_info=exception_info,
            raw_output=output,
            exit_code=exit_code,
        )

    def _render_runner_sh(self) -> str:
        """Render the complete runner.sh from the compose config.

        Uses str.replace on __PLACEHOLDER__ tokens — never str.format() —
        to safely handle bash ${var}, ${PIPESTATUS[0]}, and {} literals.
        """
        compose = self._config.compose

        runner = _RUNNER_SKELETON

        # P0 registry login
        runner = runner.replace("__REGISTRY_LOGIN__", _render_registry_login(self._config))

        # P1 OSS deps (from main container spec)
        runner = runner.replace("__OSS_DEPS__", _render_oss_deps(compose.main.oss_deps))

        # P2 init containers
        runner = runner.replace("__INIT_CONTAINERS__", _render_init_containers(compose.init_containers))

        # P3 sidecars
        runner = runner.replace("__SIDECAR_CONTAINERS__", _render_sidecar_containers(compose.sidecars))

        # P4 health probes
        runner = runner.replace("__HEALTH_PROBES__", _render_health_probes(compose.sidecars))

        # P5 main container
        runner = runner.replace("__MAIN_CONTAINER__", _render_main_container(compose.main))

        # P6 collect sidecar logs + OSS mirror upload
        runner = runner.replace("__COLLECT_SIDECAR_LOGS__", _render_collect_sidecar_logs(compose.sidecars))
        runner = runner.replace("__OSS_MIRROR_UPLOAD__", _render_oss_mirror_upload(self._config))

        # cleanup_all sidecar removal
        runner = runner.replace("__SIDECAR_CLEANUP__", _render_sidecar_cleanup(compose.sidecars))

        return runner


# Auto-register on import
register_trial(ComposeJobConfig, ComposeTrial)
