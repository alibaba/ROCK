import asyncio
import datetime
import os
import random
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
from rock.deployments.abstract import AbstractDeployment
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.constants import Port, Status
from rock.deployments.hooks.abstract import CombinedDeploymentHook, DeploymentHook
from rock.deployments.runtime_env import DockerRuntimeEnv, LocalRuntimeEnv, PipRuntimeEnv, UvRuntimeEnv
from rock.deployments.sandbox_validator import DockerSandboxValidator
from rock.deployments.status import ServiceStatus
from rock.logger import init_logger
from rock.rocklet import PACKAGE_NAME, REMOTE_EXECUTABLE_NAME
from rock.rocklet.exceptions import DeploymentNotStartedError, DockerPullError
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.utils import (
    ENV_POOL,
    DockerUtil,
    Timer,
    find_free_port,
    get_executor,
    release_port,
    sandbox_id_ctx_var,
    timeout,
    wait_until_alive,
)

__all__ = ["DockerDeployment", "DockerDeploymentConfig"]
CHECK_CLEAR_INTERVAL_SECONDS = 300


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
        self._config = DockerDeploymentConfig(**kwargs)
        self._runtime: RemoteSandboxRuntime | None = None
        self._container_process = None
        self._runtime_timeout = 0.15
        self._hooks = CombinedDeploymentHook()
        self._stop_time = datetime.datetime.now() + datetime.timedelta(minutes=self._config.auto_clear_time)
        self._check_stop_task = None
        self._container_name = None
        self._service_status = ServiceStatus()
        if self._config.container_name:
            self.set_container_name(self._config.container_name)
        if env_vars.ROCK_WORKER_ENV_TYPE == "docker":
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
        return cls(**config.model_dump())

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

    def _get_rocklet_start_cmd(self) -> list[str]:
        cmd = self._runtime_env.get_rocklet_start_cmd()

        # Need to wrap with /bin/sh -c to avoid having '&&' interpreted by the parent shell
        return [
            "/bin/sh",
            "-c",
            cmd,
        ]

    def _pull_image(self) -> None:
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
        self._hooks.on_custom_step("Pulling docker image")
        try:
            with Timer(description=f"[{self._config.image}] Image pull"):
                DockerUtil.pull_image(self._config.image)
            self._service_status.update_status(
                phase_name="image_pull", status=Status.SUCCESS, message="image pull success"
            )
        except subprocess.CalledProcessError as e:
            msg = f"Failed to pull image {self._config.image}. "
            msg += f"Error: {e.stderr.decode()}"
            msg += f"Output: {e.output.decode()}"
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
        return [f"--cpus={self.config.cpus}"]

    async def start(self):
        """Starts the runtime."""
        if not self.sandbox_validator.check_availability():
            raise Exception("Docker is not available")

        if self._container_name is None:
            self.set_container_name(self._get_container_name())
        executor = get_executor()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(executor, self._pull_image)
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

        # Conditionally set up logging path mount based on ROCK_LOGGING_PATH
        volume_args = self._prepare_volume_mounts()
        if env_vars.ROCK_LOGGING_PATH:  # Only mount if ROCK_LOGGING_PATH is set (not None or empty)
            log_file_path = f"{env_vars.ROCK_LOGGING_PATH}/{self.container_name}"
            os.makedirs(log_file_path, exist_ok=True)
            os.chmod(log_file_path, 0o777)
            volume_args.extend(["-v", f"{log_file_path}:{env_vars.ROCK_LOGGING_PATH}"])

        time.sleep(random.randint(0, 5))
        cmds = [
            "docker",
            "run",
            "--entrypoint",
            "",
            *rm_arg,
            *volume_args,
            "--privileged",
            "-p",
            f"{self._config.port}:8000",
            "-p",
            f"{self._service_status.get_mapped_port(Port.SERVER)}:8080",
            "-p",
            f"{self._service_status.get_mapped_port(Port.SSH)}:22",
            *self._memory(),
            *self._cpus(),
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
        logger.debug(f"Command: {cmd_str!r}")
        # shell=True required for && etc.
        with Timer(description=f"[{self._config.image}] Container start"):
            self._container_process = await loop.run_in_executor(executor, self._docker_run, cmds)
            await loop.run_in_executor(executor, self._hooks.on_custom_step, "Starting runtime")
            logger.info(f"Starting runtime at {self._config.port}")
            self._runtime = RemoteSandboxRuntime.from_config(
                RemoteSandboxRuntimeConfig(port=self._config.port, timeout=self._runtime_timeout)
            )
            self._runtime.set_executor(executor)
            await self._wait_until_alive(timeout=self._config.startup_timeout)
        if self._config.enable_auto_clear:
            self._check_stop_task = asyncio.create_task(self._check_stop())

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

    def _docker_run(self, cmd: list[str]):
        try:
            exec_rlt = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
            self._container_name = None

        if self._config and self._config.remove_images and DockerUtil.is_image_available(self._config.image):
            logger.info(f"Removing image {self._config.image}")
            try:
                DockerUtil.remove_image(self._config.image)
            except subprocess.CalledProcessError:
                logger.error(f"Failed to remove image {self._config.image}", exc_info=True)

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
        proxy_port = await find_free_port()
        self._service_status.add_port_mapping(Port.PROXY, proxy_port)
        ssh_port = await find_free_port()
        self._service_status.add_port_mapping(Port.SSH, ssh_port)
        server_port = await find_free_port()
        self._service_status.add_port_mapping(Port.SERVER, server_port)
        if self._config.port is None:
            self._config.port = proxy_port
