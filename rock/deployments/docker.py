import asyncio
import datetime
import hashlib
import json
import os
import random
import re
import shlex
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from typing_extensions import Self

from rock import env_vars
from rock.actions import IsAliveResponse, RemoteSandboxRuntimeConfig
from rock.common.constants import DeploymentHookStep
from rock.deployments.abstract import AbstractDeployment
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.constants import Port, Status
from rock.deployments.docker_client import TempAuthDockerClient, TempAuthDockerClientError
from rock.deployments.hooks.abstract import CombinedDeploymentHook, DeploymentHook
from rock.deployments.runtime_env import (
    ConfigurableRuntimeEnv,
    DockerRuntimeEnv,
    LocalRuntimeEnv,
    PipRuntimeEnv,
    UvRuntimeEnv,
)
from rock.deployments.sandbox_validator import DockerSandboxValidator
from rock.deployments.status import PersistedServiceStatus, ServiceStatus
from rock.logger import init_logger
from rock.rocklet import PACKAGE_NAME, REMOTE_EXECUTABLE_NAME
from rock.rocklet.exceptions import DeploymentNotStartedError, DockerPullError
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.utils import (
    ENV_POOL,
    DockerUtil,
    ImageUtil,
    StageTimer,
    find_free_port,
    get_executor,
    refresh_docker_used_ports,
    release_port,
    sandbox_id_ctx_var,
    timeout,
    wait_until_alive,
)

__all__ = ["DockerDeployment", "DockerDeploymentConfig"]
CHECK_CLEAR_INTERVAL_SECONDS = 300

XFS_PRJID_MIN = 1 << 31
XFS_PRJID_RANGE = (1 << 32) - XFS_PRJID_MIN


logger = init_logger(__name__)


class DockerDeployment(AbstractDeployment):
    def __init__(
        self,
        **kwargs: Any,
    ):
        """Deployment to local docker image.

        Args:
            **kwargs: Keyword arguments (see `DockerDeploymentConfig` for details).
        """
        registry_password = kwargs.pop("registry_password", None)
        self._config = DockerDeploymentConfig(**kwargs)
        if registry_password:
            self._config.registry_password = registry_password
        self._effective_disk: str | None = self._config.disk
        self._rootfs_xfs_prjid: int | None = None
        self._rootfs_xfs_mountpoint: str | None = None
        self._rootfs_upper_dir: str | None = None
        self._runtime: RemoteSandboxRuntime | None = None
        self._container_process = None
        self._runtime_timeout = 0.15
        self._hooks = CombinedDeploymentHook()
        self._stop_time = datetime.datetime.now() + datetime.timedelta(minutes=self._config.auto_clear_time)
        self._check_stop_task = None
        self._container_name = None
        self._service_status = PersistedServiceStatus()

        if self._config.container_name:
            self.set_container_name(self._config.container_name)
        if self._config.image_os_profile:
            self._runtime_env = ConfigurableRuntimeEnv(self._config.image_os_profile.get("runtime_env", {}))
        elif env_vars.ROCK_WORKER_ENV_TYPE == "docker":
            self._runtime_env = DockerRuntimeEnv()
        elif env_vars.ROCK_WORKER_ENV_TYPE == "local":
            self._runtime_env = LocalRuntimeEnv(self._config.runtime_config)
        elif env_vars.ROCK_WORKER_ENV_TYPE == "uv":
            self._runtime_env = UvRuntimeEnv(self._config.runtime_config)
        elif env_vars.ROCK_WORKER_ENV_TYPE == "pip":
            self._runtime_env = PipRuntimeEnv(self._config.runtime_config)
        else:
            raise Exception(f"Invalid ROCK_WORKER_ENV_TYPE: {env_vars.ROCK_WORKER_ENV_TYPE}")

        self.sandbox_validator: DockerSandboxValidator | None = DockerSandboxValidator()

    def add_hook(self, hook: DeploymentHook):
        self._hooks.add_hook(hook)

    def set_container_name(self, name: str):
        if self._container_name is None:
            self._container_name = name
            sandbox_id_ctx_var.set(name)
        else:
            logger.warning(f"container name {self._container_name} already exists")

    @classmethod
    def from_config(cls, config: DockerDeploymentConfig) -> Self:
        return cls(**config.model_dump(), registry_password=config.registry_password)

    def _get_container_name(self) -> str:
        """Returns a unique container name based on the image name."""
        image_name_sanitized = "".join(c for c in self._config.image if c.isalnum() or c in "-_.")
        return f"{image_name_sanitized}-{uuid.uuid4()}"

    @property
    def container_name(self) -> str | None:
        return self._container_name

    @property
    def pod_name(self) -> str | None:
        return Path("/etc/hostname").read_text().strip() if Path("/etc/hostname").exists() else None

    async def is_alive(self, *, timeout: float | None = None) -> IsAliveResponse:
        """Checks if the runtime is alive. The return value can be
        tested with bool().

        Raises:
            DeploymentNotStartedError: If the deployment was not started.
        """
        if self._runtime is None:
            msg = "Runtime not started"
            raise RuntimeError(msg)
        if self._container_process is None:
            msg = "Container process not started"
            raise RuntimeError(msg)
        if self._container_process.poll() is not None:
            msg = "Container process terminated."
            output = "stdout:\n" + self._container_process.stdout.read().decode()  # type: ignore
            output += "\nstderr:\n" + self._container_process.stderr.read().decode()  # type: ignore
            msg += "\n" + output
            raise RuntimeError(msg)
        return await self._runtime.is_alive(timeout=timeout)

    async def _wait_until_alive(self, timeout: float = 10.0):
        try:
            await wait_until_alive(self.is_alive, timeout=timeout, function_timeout=self._runtime_timeout)
            self._service_status.update_status(
                phase_name="docker_run", status=Status.SUCCESS, message="docker run success"
            )
            return
        except TimeoutError as e:
            logger.error("Runtime did not start within timeout. Here's the output from the container process.")
            assert self._container_process is not None
            self._service_status.update_status(
                phase_name="docker_run", status=Status.TIMEOUT, message="docker run timeout"
            )
            await self.stop()
            raise e

    def _get_token(self) -> str:
        return str(uuid.uuid4())

    def _build_runtime_args(self) -> list[str]:
        """Build runtime-specific docker arguments.

        Returns kata runtime args if use_kata_runtime is enabled,
        otherwise returns the default --privileged flag.
        """
        if self._config.use_kata_runtime:
            return [
                "--cap-add=ALL",
                "--security-opt",
                "seccomp=unconfined",
                "--runtime=io.containerd.kata.v2",
                "--sysctl",
                "net.ipv4.ip_forward=1",
            ]
        return ["--privileged"]

    def _get_kata_disk_image_path(self) -> str:
        """Returns the host path for the kata disk image file."""
        return os.path.join(self._config.kata_disk_base_path, f"{self._container_name}.img")

    def _prepare_kata_disk(self) -> None:
        """Create and format a sparse disk image for kata DinD on the host.

        Only called when use_kata_runtime is enabled. Creates a sparse file
        using truncate (no actual disk space consumed until written) and
        formats it as ext4.
        """
        if not self._config.use_kata_runtime:
            return

        disk_path = self._get_kata_disk_image_path()
        os.makedirs(self._config.kata_disk_base_path, exist_ok=True)
        logger.info(f"Creating kata disk image: {disk_path} (size={self._config.kata_disk_size})")

        try:
            subprocess.check_call(
                ["truncate", "-s", self._config.kata_disk_size, disk_path],
                timeout=10,
            )
            subprocess.check_call(
                ["mkfs.ext4", "-F", disk_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            logger.info(f"Kata disk image created and formatted: {disk_path}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"Failed to prepare kata disk image {disk_path}: {e}", exc_info=True)
            if os.path.exists(disk_path):
                os.remove(disk_path)
            raise

    def _cleanup_kata_disk(self) -> None:
        """Remove the kata disk image file from the host.

        Only called when use_kata_runtime is enabled. Silently ignores
        missing files to handle cases where preparation failed.
        """
        if not self._config or not self._config.use_kata_runtime:
            return
        if not self._container_name:
            return

        disk_path = self._get_kata_disk_image_path()
        try:
            if os.path.exists(disk_path):
                os.remove(disk_path)
                logger.info(f"Kata disk image removed: {disk_path}")
        except OSError as e:
            logger.warning(f"Failed to remove kata disk image {disk_path}: {e}", exc_info=False)

    def _get_rocklet_start_cmd(self) -> list[str]:
        cmd = self._runtime_env.get_rocklet_start_cmd()

        # Need to wrap with /bin/sh -c to avoid having '&&' interpreted by the parent shell
        return [
            "/bin/sh",
            "-c",
            cmd,
        ]

    def _pull_image(self, pull_timeout: int = 600) -> None:
        """Pull image using temporary authentication.

        Uses TempAuthDockerClient to ensure credentials are isolated
        and automatically cleaned up after the pull operation.
        """
        if self._config.pull == "never":
            self._service_status.update_status(
                phase_name="image_pull", status=Status.SUCCESS, message="skip image pull"
            )
            return

        if self._config.pull == "missing" and DockerUtil.is_image_available(self._config.image):
            self._service_status.update_status(
                phase_name="image_pull", status=Status.SUCCESS, message="use cached image, skip image pull"
            )
            return

        self._service_status.update_status(phase_name="image_pull", status=Status.RUNNING, message="image pull running")
        logger.info(f"Pulling image {self._config.image!r}")

        try:
            with StageTimer("startup_timing", f"[{self._container_name}] [{self._config.image}] Image pull", logger):
                # Parse registry from image name
                registry, _ = ImageUtil.parse_registry_and_others(self._config.image)

                # Create temp auth client with credentials if available
                with TempAuthDockerClient(
                    registry=registry if self._config.registry_username else None,
                    username=self._config.registry_username,
                    password=self._config.registry_password,
                ) as client:
                    client.pull(self._config.image, timeout=pull_timeout)

            self._service_status.update_status(
                phase_name="image_pull", status=Status.SUCCESS, message="image pull success"
            )

        except (subprocess.CalledProcessError, TempAuthDockerClientError) as e:
            msg = f"Failed to pull image {self._config.image}: {e}"
            self._service_status.update_status(phase_name="image_pull", status=Status.FAILED, message=msg)
            raise DockerPullError(msg) from e

    @property
    def glibc_dockerfile(self) -> str:
        # will only work with glibc-based systems
        if self._config.platform:
            platform_arg = f"--platform={self._config.platform}"
        else:
            platform_arg = ""
        return (
            "ARG BASE_IMAGE\n\n"
            # Build stage for standalone Python
            f"FROM {platform_arg} python:3.11-slim AS builder\n"
            # Install build dependencies
            "RUN apt-get update && apt-get install -y \\\n"
            "    wget \\\n"
            "    gcc \\\n"
            "    make \\\n"
            "    zlib1g-dev \\\n"
            "    libssl-dev \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
            # Download and compile Python as standalone
            "WORKDIR /build\n"
            "RUN wget https://www.python.org/ftp/python/3.11.8/Python-3.11.8.tgz \\\n"
            "    && tar xzf Python-3.11.8.tgz\n"
            "WORKDIR /build/Python-3.11.8\n"
            "RUN ./configure \\\n"
            "    --prefix=/root/python3.11 \\\n"
            "    --enable-shared \\\n"
            "    LDFLAGS='-Wl,-rpath=/root/python3.11/lib' && \\\n"
            "    make -j$(nproc) && \\\n"
            "    make install && \\\n"
            "    ldconfig\n\n"
            # Production stage
            f"FROM {platform_arg} $BASE_IMAGE\n"
            # Ensure we have the required runtime libraries
            "RUN apt-get update && apt-get install -y \\\n"
            "    libc6 \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
            # Copy the standalone Python installation
            f"COPY --from=builder /root/python3.11 {self._config.python_standalone_dir}/python3.11\n"
            f"ENV LD_LIBRARY_PATH={self._config.python_standalone_dir}/python3.11/lib:${{LD_LIBRARY_PATH:-}}\n"
            # Verify installation
            f"RUN {self._config.python_standalone_dir}/python3.11/bin/python3 --version\n"
            # Install rocklet using the standalone Python
            f"RUN /root/python3.11/bin/pip3 install --no-cache-dir {PACKAGE_NAME}\n\n"
            f"RUN ln -s /root/python3.11/bin/{REMOTE_EXECUTABLE_NAME} /usr/local/bin/{REMOTE_EXECUTABLE_NAME}\n\n"
            f"RUN {REMOTE_EXECUTABLE_NAME} --version\n"
        )

    def _build_image(self) -> str:
        """Builds image, returns image ID."""
        logger.info(
            f"Building image {self._config.image} to install a standalone python to {self._config.python_standalone_dir}. "
            "This might take a while (but you only have to do it once). To skip this step, set `python_standalone_dir` to None."
        )
        dockerfile = self.glibc_dockerfile
        platform_arg = []
        if self._config.platform:
            platform_arg = ["--platform", self._config.platform]
        build_cmd = [
            "docker",
            "build",
            "-q",
            *platform_arg,
            "--build-arg",
            f"BASE_IMAGE={self._config.image}",
            "-",
        ]
        image_id = (
            subprocess.check_output(
                build_cmd,
                input=dockerfile.encode(),
            )
            .decode()
            .strip()
        )
        if not image_id.startswith("sha256:"):
            msg = f"Failed to build image. Image ID is not a SHA256: {image_id}"
            raise RuntimeError(msg)
        return image_id

    def _memory(self):
        return [f"--memory={self.config.memory}", f"--memory-swap={self.config.memory}"]

    def _cpus(self):
        if self.config.limit_cpus is not None:
            cpu_shares = int(self.config.cpus * 1024)
            return [f"--cpu-shares={cpu_shares}", f"--cpus={self.config.limit_cpus}"]
        return [f"--cpus={self.config.cpus}"]

    def _storage_opts(self):
        if self._effective_disk is not None:
            return ["--storage-opt", f"size={self._effective_disk}"]
        return []

    def _get_docker_rootfs_prjid_and_upper_dir(self) -> tuple[int | None, str | None]:
        """Read the XFS project id docker assigned to the container's overlay2
        upper dir, together with the upper dir path itself.

        Only meaningful after `docker create` (the upper dir exists by then).
        Returns (None, None) on any failure — the caller should fall back to
        an independent prjid path.
        """
        try:
            inspect_result = subprocess.run(
                ["docker", "inspect", "--format={{.GraphDriver.Data.UpperDir}}", self._container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if inspect_result.returncode != 0 or not inspect_result.stdout.strip():
                return None, None
            upper_dir = inspect_result.stdout.strip()

            lsproj_result = subprocess.run(
                ["xfs_io", "-r", "-c", "lsproj", upper_dir],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if lsproj_result.returncode != 0:
                return None, None
            # output looks like: `projid = 12345` (or similar). Grab the trailing integer.
            match = re.search(r"(\d+)\s*$", lsproj_result.stdout.strip())
            if not match:
                return None, None
            prjid = int(match.group(1))
            if prjid <= 0:
                return None, None
            return prjid, upper_dir
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"Failed to read docker rootfs prjid for {self._container_name}: {e}")
            return None, None

    def _setup_log_dir_quota_shared(self, log_file_path: str) -> None:
        """Attach the docker-allocated rootfs prjid to the host log dir so that
        rootfs and log share a single XFS quota (the bhard already set by
        `--storage-opt size=`).

        Must be called between `docker create` and `docker start`. On any
        failure (no rootfs prjid, log dir on a different filesystem, quota
        command error) the log dir is simply left unbound.

        We only `project -s` here (binds prjid + sets inherit flag); we do
        NOT call `limit -p`. The bhard belongs to docker; setting a separate
        limit would clobber it. Cleanup is also docker's responsibility:
        `docker rm` resets the prjid's limit when the upper dir is torn down.
        """
        if self._rootfs_xfs_prjid is not None and self._rootfs_upper_dir is not None:
            project_id, upper_dir = self._rootfs_xfs_prjid, self._rootfs_upper_dir
        elif self._effective_disk is not None:
            project_id, upper_dir = self._get_docker_rootfs_prjid_and_upper_dir()
        else:
            return

        logger.info(f"setup_log_dir_quota_shared: log={log_file_path!r}, prjid={project_id}, upper_dir={upper_dir!r}")
        if project_id is None or upper_dir is None:
            logger.info(f"docker rootfs prjid unavailable for {log_file_path!r}; cannot share, fall back")
            return

        try:
            if os.stat(log_file_path).st_dev != os.stat(upper_dir).st_dev:
                logger.info(
                    f"log dir {log_file_path!r} on different filesystem from rootfs {upper_dir!r}; "
                    f"cannot share prjid, fall back"
                )
                return
        except OSError as e:
            logger.warning(f"stat failed while checking prjid sharing for {log_file_path!r}: {e}")
            return

        try:
            findmnt_result = subprocess.run(
                ["findmnt", "-T", upper_dir, "-o", "TARGET", "--noheadings"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if findmnt_result.returncode != 0 or not findmnt_result.stdout.strip():
                logger.warning(
                    f"findmnt failed for upper_dir {upper_dir!r}: {findmnt_result.stderr.strip() or 'empty output'}"
                )
                return
            xfs_mountpoint = findmnt_result.stdout.strip()

            set_project_cmd = f"project -s -p {shlex.quote(log_file_path)} {project_id}"
            result = subprocess.run(
                ["xfs_quota", "-x", "-c", set_project_cmd, xfs_mountpoint],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    f"xfs_quota project -s failed for {log_file_path!r} prjid={project_id}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
                return

            logger.info(
                f"Attached log dir {log_file_path!r} to docker rootfs prjid={project_id} "
                f"(shared rootfs+log quota, limit managed by docker --storage-opt)"
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"Failed to share rootfs prjid with log dir {log_file_path!r}: {e}")

    def _get_container_upper_dir(self) -> str | None:
        """Return the overlay UpperDir for the container.

        Strategy 1 — ``docker inspect`` (overlay2, available after create):
        Strategy 2 — ``/proc/mounts`` (containerd image store, available after start):
        Returns None if both fail.
        """
        # Strategy 1: docker inspect
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format={{.GraphDriver.Data.UpperDir}}", self._container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            upper = result.stdout.strip()
            if result.returncode == 0 and upper and upper != "<no value>":
                return upper
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass

        # Strategy 2: /proc/mounts — overlay mounts whose target path contains the container id
        try:
            container_id_result = subprocess.run(
                ["docker", "inspect", "--format={{.Id}}", self._container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if container_id_result.returncode != 0 or not container_id_result.stdout.strip():
                return None
            container_id = container_id_result.stdout.strip()

            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    fstype, mount_target, options = parts[2], parts[1], parts[3]
                    if fstype != "overlay" or container_id not in mount_target:
                        continue
                    for opt in options.split(","):
                        if opt.startswith("upperdir="):
                            return opt[len("upperdir=") :]
        except OSError:
            pass
        return None

    def _setup_rootfs_quota_xfs(self) -> None:
        """Set XFS project quota on the container's UpperDir as a fallback.

        Called after ``docker start`` when ``--storage-opt`` is not supported
        but the user configured ``disk_limit_rootfs``.  Best-effort: any
        failure logs a warning and silently skips quota setup.
        """
        upper_dir = self._get_container_upper_dir()
        if not upper_dir:
            logger.warning(f"[{self._container_name}] cannot find container UpperDir, skipping XFS quota fallback")
            return

        if not DockerUtil.is_xfs_prjquota_path(upper_dir):
            logger.info(f"[{self._container_name}] UpperDir {upper_dir!r} not on xfs+prjquota, skipping XFS quota")
            return

        try:
            prjid = (
                int(hashlib.sha1(self._container_name.encode()).hexdigest()[:8], 16) % XFS_PRJID_RANGE
            ) + XFS_PRJID_MIN

            findmnt_result = subprocess.run(
                ["findmnt", "-T", upper_dir, "-o", "TARGET", "--noheadings"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if findmnt_result.returncode != 0 or not findmnt_result.stdout.strip():
                logger.warning(f"[{self._container_name}] findmnt failed for {upper_dir!r}, skipping XFS quota")
                return
            mountpoint = findmnt_result.stdout.strip()

            set_project_cmd = f"project -s -p {shlex.quote(upper_dir)} {prjid}"
            result = subprocess.run(
                ["xfs_quota", "-x", "-c", set_project_cmd, mountpoint],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    f"[{self._container_name}] xfs_quota project -s failed: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
                return

            limit_cmd = f"limit -p bhard={self._config.disk} {prjid}"
            result = subprocess.run(
                ["xfs_quota", "-x", "-c", limit_cmd, mountpoint],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    f"[{self._container_name}] xfs_quota limit failed: {result.stderr.strip() or result.stdout.strip()}"
                )
                return

            self._rootfs_xfs_prjid = prjid
            self._rootfs_xfs_mountpoint = mountpoint
            self._rootfs_upper_dir = upper_dir
            logger.info(
                f"[{self._container_name}] XFS quota fallback: prjid={prjid}, "
                f"bhard={self._config.disk}, upper_dir={upper_dir!r}"
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"[{self._container_name}] XFS quota fallback failed: {e}")

    def _cleanup_rootfs_xfs_quota(self) -> None:
        """Remove XFS project quota set by ``_setup_rootfs_quota_xfs``."""
        if self._rootfs_xfs_prjid is None or self._rootfs_xfs_mountpoint is None:
            return
        prjid = self._rootfs_xfs_prjid
        mountpoint = self._rootfs_xfs_mountpoint

        def _run_xfs_quota(cmd: str) -> None:
            try:
                subprocess.run(
                    ["xfs_quota", "-x", "-c", cmd, mountpoint],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                logger.warning(f"[{self._container_name}] xfs_quota cleanup ({cmd!r}) failed: {e}")

        _run_xfs_quota(f"limit -p bhard=0 bsoft=0 {prjid}")

        if self._rootfs_upper_dir:
            _run_xfs_quota(f"project -C -p {shlex.quote(self._rootfs_upper_dir)} {prjid}")

        if self._container_name and env_vars.ROCK_LOGGING_PATH:
            log_dir = f"{env_vars.ROCK_LOGGING_PATH}/{self._container_name}"
            _run_xfs_quota(f"project -C -p {shlex.quote(log_dir)} {prjid}")

        self._rootfs_xfs_prjid = None
        self._rootfs_xfs_mountpoint = None
        self._rootfs_upper_dir = None

    def _setup_local_volumes_quota_shared(self) -> None:
        """Bind anonymous volumes (from Dockerfile VOLUME) to the rootfs XFS
        prjid so they share the ``--storage-opt size=`` bhard limit.

        Must be called between ``docker create`` and ``docker start``.
        Fail-soft: any error is logged and skipped (consistent with
        ``_setup_log_dir_quota_shared``).
        """
        if self._effective_disk is None:
            return

        project_id, upper_dir = self._get_docker_rootfs_prjid_and_upper_dir()
        if project_id is None or upper_dir is None:
            return

        try:
            inspect_result = subprocess.run(
                ["docker", "inspect", "--format={{json .Mounts}}", self._container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if inspect_result.returncode != 0 or not inspect_result.stdout.strip():
                return
            mounts = json.loads(inspect_result.stdout.strip())
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to inspect mounts for {self._container_name}: {e}")
            return

        volume_sources = [m["Source"] for m in mounts if m.get("Type") == "volume"]
        if not volume_sources:
            return

        try:
            upper_dev = os.stat(upper_dir).st_dev
        except OSError as e:
            logger.warning(f"stat failed for upper_dir {upper_dir!r}: {e}")
            return

        try:
            findmnt_result = subprocess.run(
                ["findmnt", "-T", upper_dir, "-o", "TARGET", "--noheadings"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if findmnt_result.returncode != 0 or not findmnt_result.stdout.strip():
                logger.warning(f"findmnt failed for upper_dir {upper_dir!r}")
                return
            xfs_mountpoint = findmnt_result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"findmnt command failed for upper_dir {upper_dir!r}: {e}")
            return

        for vol_source in volume_sources:
            try:
                if os.stat(vol_source).st_dev != upper_dev:
                    logger.info(f"Volume {vol_source!r} on different filesystem from rootfs, skipping quota")
                    continue
            except OSError as e:
                logger.warning(f"stat failed for volume source {vol_source!r}: {e}")
                continue

            set_project_cmd = f"project -s -p {shlex.quote(vol_source)} {project_id}"
            try:
                result = subprocess.run(
                    ["xfs_quota", "-x", "-c", set_project_cmd, xfs_mountpoint],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    logger.warning(
                        f"xfs_quota project -s failed for volume {vol_source!r} prjid={project_id}: "
                        f"{result.stderr.strip() or result.stdout.strip()}"
                    )
                    continue
                logger.info(
                    f"Attached volume {vol_source!r} to rootfs prjid={project_id} "
                    f"(shared rootfs+volume quota, limit managed by docker --storage-opt)"
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                logger.warning(f"Failed to share rootfs prjid with volume {vol_source!r}: {e}")

    async def start(self):
        """Starts the runtime."""
        with StageTimer("startup_timing", f"[{self._container_name}] Check availability", logger):
            if not self.sandbox_validator.check_availability():
                raise Exception("Docker is not available")

        storage_opt_supported = DockerUtil.detect_storage_opt_support()
        # Resolve effective rootfs quota: downgrade to None if storage-opt is not supported.
        if self._config.disk is not None and not storage_opt_supported:
            logger.warning(
                f"[{self.config.container_name}] --storage-opt not supported on this worker "
                f"(requires overlay2 + xfs + prjquota), ignoring disk={self._config.disk}"
            )
            self._effective_disk = None
        else:
            self._effective_disk = self._config.disk

        if self._container_name is None:
            self.set_container_name(self._get_container_name())
        self._service_status.set_sandbox_id(self._container_name)
        executor = get_executor()
        loop = asyncio.get_running_loop()
        startup_timeout = self._config.startup_timeout or 600.0
        deadline = time.monotonic() + startup_timeout

        await loop.run_in_executor(executor, self._pull_image, int(startup_timeout))
        if self._config.python_standalone_dir is not None:
            image_id = self._build_image()
        else:
            image_id = self._config.image

        if not self.sandbox_validator.check_resource(image_id):
            raise Exception(f"Image {image_id} is not valid")

        await self.do_port_mapping()
        platform_arg = []
        if self._config.platform is not None:
            platform_arg = ["--platform", self._config.platform]
        rm_arg = []
        if self._config.remove_container:
            rm_arg = ["--rm"]

        # Conditionally set up logging path mount based on ROCK_LOGGING_PATH.
        log_file_path: str | None = None
        volume_args = self._prepare_volume_mounts()
        if env_vars.ROCK_LOGGING_PATH:  # Only mount if ROCK_LOGGING_PATH is set (not None or empty)
            log_file_path = f"{env_vars.ROCK_LOGGING_PATH}/{self.container_name}"
            os.makedirs(log_file_path, exist_ok=True)
            os.chmod(log_file_path, 0o777)
            volume_args.extend(["-v", f"{log_file_path}:{env_vars.ROCK_LOGGING_PATH}"])

        volume_args.extend(self._prepare_timezone_mount())

        # Kata DinD: prepare disk image and add volume mount
        if self._config.use_kata_runtime:
            with StageTimer("startup_timing", f"[{self._container_name}] Kata disk prepare", logger):
                self._prepare_kata_disk()
            disk_path = self._get_kata_disk_image_path()
            volume_args.extend(["-v", f"{disk_path}:/docker-disk.img"])

        env_arg = self._build_env_args()

        with StageTimer("startup_timing", f"[{self._container_name}] Random sleep", logger):
            time.sleep(random.randint(0, 5))
        runtime_args = self._build_runtime_args()
        cmds = [
            "docker",
            "create",
            "--entrypoint",
            "",
            *env_arg,
            *rm_arg,
            *volume_args,
            *runtime_args,
            "-p",
            f"{self._config.port}:{Port.PROXY}",
            "-p",
            f"{self._service_status.get_mapped_port(Port.SERVER)}:8080",
            "-p",
            f"{self._service_status.get_mapped_port(Port.SSH)}:22",
            *self._memory(),
            *self._cpus(),
            *self._storage_opts(),
            *platform_arg,
            *self._config.docker_args,
            "--name",
            self._container_name,
            image_id,
            *self._get_rocklet_start_cmd(),
        ]
        cmd_str = shlex.join(cmds)
        logger.info(
            f"Starting container {self._container_name} with image {self._config.image} serving on port {self._config.port}"
        )
        logger.info(f"Command: {cmd_str!r}")
        # shell=True required for && etc.
        with StageTimer("startup_timing", f"[{self._container_name}] Docker start", logger):
            await loop.run_in_executor(executor, self._docker_create, cmds)
            # After docker create succeeds, the container exists in `created` state. If anything below
            # fails before _wait_until_alive sets _container_process up for _stop to manage, we own the
            # orphan and must remove it. _wait_until_alive's own failure path already calls self.stop().
            try:
                is_containerd = DockerUtil.detect_containerd_image_store()

                # [overlay2] Bind log dir to the docker-allocated rootfs prjid in the
                # create→start gap (prjid exists after docker create).
                if log_file_path is not None and not is_containerd:
                    self._setup_log_dir_quota_shared(log_file_path)
                self._setup_local_volumes_quota_shared()

                self._container_process = await loop.run_in_executor(executor, self._docker_start)

                # [containerd fallback] --storage-opt is silently ignored by containerd,
                # apply XFS project quota on the container's UpperDir instead.
                if self._config.disk is not None and is_containerd:
                    self._setup_rootfs_quota_xfs()
                    if log_file_path is not None:
                        self._setup_log_dir_quota_shared(log_file_path)
            except Exception:
                DockerUtil.remove_container_force(self._container_name)
                raise
        await loop.run_in_executor(executor, self._hooks.on_custom_step, DeploymentHookStep.STARTING_RUNTIME)
        logger.info(f"Starting runtime at {self._config.port}")
        self._runtime = RemoteSandboxRuntime.from_config(
            RemoteSandboxRuntimeConfig(port=self._config.port, timeout=self._runtime_timeout)
        )
        self._runtime.set_executor(executor)
        remaining = max(deadline - time.monotonic(), 1.0)
        with StageTimer("startup_timing", f"[{self._container_name}] Wait until alive", logger):
            await self._wait_until_alive(timeout=remaining)
        if self._config.enable_auto_clear:
            self._check_stop_task = asyncio.create_task(self._check_stop())

    def _build_env_args(self) -> list[str]:
        """Construct `-e KEY=VALUE` argv pairs for `docker run`.

        Centralizes every env var rock injects into a sandbox in one place so
        callers (and tests) can audit them together. Volume mounts that pair
        with these vars (ROCK_LOGGING_PATH, kata disk) are still set up
        alongside in `start()`.
        """
        args: list[str] = []
        if env_vars.ROCK_LOGGING_PATH:
            args.extend(
                [
                    "-e",
                    f"ROCK_LOGGING_PATH={env_vars.ROCK_LOGGING_PATH}",
                    "-e",
                    f"ROCK_LOGGING_LEVEL={env_vars.ROCK_LOGGING_LEVEL}",
                ]
            )
        args.extend(["-e", f"ROCK_TIME_ZONE={env_vars.ROCK_TIME_ZONE}"])
        if self._config.use_kata_runtime:
            args.extend(["-e", "ROCK_KATA_RUNTIME=true"])
        mirrors = self._config.runtime_config.instance_registry_mirrors
        if mirrors:
            args.extend(["-e", f"INSTANCE_ROCK_REGISTRY={','.join(mirrors)}"])
        args.extend(self._runtime_env.get_extra_env_args(self._config))
        return args

    def _prepare_timezone_mount(self) -> list[str]:
        tz = env_vars.ROCK_TIME_ZONE
        localtime_src = f"/usr/share/zoneinfo/{tz}"
        if os.path.isfile(localtime_src):
            return ["-v", f"{localtime_src}:/etc/localtime:ro"]
        logger.warning(f"Zoneinfo file not found: {localtime_src}, skipping /etc/localtime mount")
        return []

    def _prepare_volume_mounts(self) -> list[str]:
        mount_configs = self._runtime_env.get_volume_mounts()

        volume_args = []

        for config in mount_configs:
            local_path = config["local"]
            container_path = config["container"]
            if os.path.exists(local_path):
                volume_args.extend(["-v", f"{local_path}:{container_path}:ro"])

        logger.info(f"volume_args: {volume_args}")
        return volume_args

    def _docker_create(self, cmd: list[str]) -> None:
        """Create the container without starting it."""
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            logger.error(f"Failed to create container {self._container_name}")
            self._service_status.update_status(
                phase_name="docker_run", status=Status.FAILED, message="docker run failed"
            )
            raise

    def _docker_start(self) -> subprocess.Popen:
        """Start a previously-created container with stdout/stderr attached."""
        try:
            exec_rlt = subprocess.Popen(
                ["docker", "start", "-a", self._container_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._service_status.update_status(
                phase_name="docker_run", status=Status.RUNNING, message="docker run running"
            )
            return exec_rlt
        except:  # Catch exception
            logger.error(f"Failed to start container {self._container_name}")
            self._service_status.update_status(
                phase_name="docker_run", status=Status.FAILED, message="docker run failed"
            )
            raise

    async def stop(self):
        stop_executor = ThreadPoolExecutor(max_workers=3)
        loop = asyncio.get_running_loop()
        if self._runtime:
            await loop.run_in_executor(stop_executor, self._stop)

    async def restart(self):
        """Restart an existing stopped container using docker start.

        Precondition: caller (SandboxStateMachine) guarantees the container
        is in a stopped/exited state. A nonexistent container surfaces via
        `docker start` failing — see is_alive()'s poll-based detection.
        """
        # TODO: once a sandbox delete API exists, move _cleanup_kata_disk() there;
        # until then kata restart is blocked because _stop() deletes the .img file.
        if self._config.use_kata_runtime:
            raise NotImplementedError(
                f"Restart is not supported for kata runtime containers (container={self._container_name}). "
            )

        executor = get_executor()
        loop = asyncio.get_running_loop()

        logger.info(f"Restarting container {self._container_name} with docker start")

        # Reuse the same Popen-based attached start used by start(), so the
        # restart path also produces a valid self._container_process. Without
        # this, _stop() would skip its `if self._container_process is not None`
        # branch and never call docker kill / cleanup.
        self._container_process = await loop.run_in_executor(executor, self._docker_start)

        # Recover port mappings from the persisted service status file.
        # When a new actor is created for restart, _service_status is empty,
        # but the file written by the original actor during start() is still on disk.
        self._service_status.set_sandbox_id(self._container_name)
        status_path = PersistedServiceStatus.gen_service_status_path(self._container_name)
        if os.path.exists(status_path):
            with open(status_path) as f:
                data = json.load(f)
            for port_value, mapping in data.get("port_mapping", {}).items():
                self._service_status.add_port_mapping(int(port_value), mapping)

        if self._config.port is None:
            self._config.port = self._service_status.port_mapping.get(Port.PROXY)
        if self._config.port is None:
            raise Exception(f"Cannot determine rocklet port for container {self._container_name}")

        # Re-establish runtime connection
        logger.info(f"Starting runtime at {self._config.port}")
        self._runtime = RemoteSandboxRuntime.from_config(
            RemoteSandboxRuntimeConfig(port=self._config.port, timeout=self._runtime_timeout)
        )
        self._runtime.set_executor(executor)

        # Wait until container is alive
        with StageTimer("startup_timing", f"[{self._container_name}] Wait until alive", logger):
            await self._wait_until_alive(timeout=self._config.startup_timeout or 600.0)

        # Re-enable auto-clear if configured
        if self._config.enable_auto_clear:
            self._check_stop_task = asyncio.create_task(self._check_stop())

        logger.info(f"Container {self._container_name} restarted successfully")

    async def delete(self) -> None:
        """Remove the container via ``docker rm -f``.

        Idempotent — a container that doesn't exist counts as success because
        nothing remains to clean up. The actor was previously stopped (or
        freshly created without start), so there is no
        ``self._container_process`` / ``self._runtime`` to unwind here.
        Quota / log cleanup already ran during ``_stop`` and is not repeated.
        """
        container_name = self._container_name or (self._config.container_name if self._config else None)
        if not container_name:
            logger.warning("delete: no container_name available, skipping docker rm")
            return

        executor = get_executor()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(executor, DockerUtil.remove_container_force, container_name)

    def _get_rocklet_port_from_inspect(self) -> int | None:
        """Read the host-side port mapped to the rocklet (container port 22555) from docker inspect."""
        try:
            result = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{{(index (index .HostConfig.PortBindings "22555/tcp") 0).HostPort}}',
                    self._container_name,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                port_str = result.stdout.strip()
                if port_str.isdigit():
                    return int(port_str)
        except Exception as e:
            logger.warning(f"Failed to get rocklet port from inspect for {self._container_name}: {e}")
        return None

    def _stop(self):
        """Stops the runtime."""
        if self._container_name in ENV_POOL:
            del ENV_POOL[self._container_name]
        if self._runtime is not None:
            try:
                with timeout(5):
                    self._runtime.close()
            except TimeoutError as e:
                logger.error("close timeout", exc_info=e)
            except Exception as e:
                logger.error("close failed", exc_info=e)
            self._runtime = None

        if self._container_process is not None:
            try:
                subprocess.check_call(
                    ["docker", "kill", self._container_name],  # type: ignore
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                logger.warning(
                    f"Failed to kill container {self._container_name}: {e}. Will try harder.", exc_info=False
                )
            for _ in range(3):
                self._container_process.kill()
                try:
                    self._container_process.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
            else:
                logger.warning(f"Failed to kill container {self._container_name} with SIGKILL")

            self._container_process = None
            self._cleanup_rootfs_xfs_quota()
            self._cleanup_kata_disk()
            self._container_name = None

        if self._check_stop_task is not None:
            logger.info("Stopping check task")
            self._check_stop_task.cancel()
            self._check_stop_task = None

        service_status = self.get_status()
        for _, port in service_status.get_port_mapping().items():
            release_port(port)

        self._config = None

    @property
    def runtime(self) -> RemoteSandboxRuntime:
        """Returns the runtime if running.

        Raises:
            DeploymentNotStartedError: If the deployment was not started.
        """
        if self._runtime is None:
            raise DeploymentNotStartedError()
        return self._runtime

    @property
    def config(self) -> DockerDeploymentConfig:
        """Returns the config of the deployment."""
        return self._config

    @property
    def effective_disk(self) -> str | None:
        """Returns the actual rootfs quota in effect after runtime capability checks (may differ from config.disk)."""
        return self._effective_disk

    async def _check_stop(self):
        logger.info(f"Start check container to stop: {self._container_name}")
        try:
            while True:
                if self._stop_time < datetime.datetime.now():
                    logger.info("Start stopping container")
                    if self._config is not None:
                        await self.stop()
                    break
                await asyncio.sleep(CHECK_CLEAR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            # When the task is canceled (whether externally canceled or self-canceled), this exception will be caught
            logger.info(
                f"Automatic cleanup coroutine [container name: {self._container_name}]: Received cancellation request, exiting gracefully..."
            )
        except Exception as e:
            # Catch other possible exceptions
            logger.info(f"Automatic cleanup coroutine [container name: {self._container_name}]: Error occurred: {e}")
        finally:
            # Whether the task ends normally (usually not, because it's an infinite loop) or is canceled, cleanup operations will be performed
            if self._check_stop_task is not None:
                logger.info("Stopping check task")
                self._check_stop_task.cancel()
            logger.info("Automatic cleanup coroutine: Resource cleanup or final processing completed.")

    async def refresh_stop_time(self):
        self._stop_time = datetime.datetime.now() + datetime.timedelta(minutes=self._config.auto_clear_time)
        logger.debug(f"Refresh stop time: {self._stop_time}")

    def get_status(self) -> ServiceStatus:
        return self._service_status

    async def do_port_mapping(self):
        refresh_docker_used_ports()
        proxy_port = await find_free_port()
        self._service_status.add_port_mapping(Port.PROXY, proxy_port)
        ssh_port = await find_free_port()
        self._service_status.add_port_mapping(Port.SSH, ssh_port)
        server_port = await find_free_port()
        self._service_status.add_port_mapping(Port.SERVER, server_port)
        if self._config.port is None:
            self._config.port = proxy_port
