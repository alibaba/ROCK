import asyncio
import datetime
import os
import socket
import subprocess
import tempfile

import ray
from fastapi import UploadFile

from rock.actions import (
    BashObservation,
    CloseBashSessionResponse,
    CommandResponse,
    CreateBashSessionResponse,
    IsAliveResponse,
    ReadFileResponse,
    UploadResponse,
    WriteFileResponse,
)
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.proto.request import SandboxBashAction as BashAction
from rock.admin.proto.request import SandboxCloseBashSessionRequest as CloseBashSessionRequest
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.proto.request import SandboxCreateBashSessionRequest as CreateBashSessionRequest
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.admin.proto.request import SandboxWriteFileRequest as WriteFileRequest
from rock.deployments.abstract import AbstractDeployment
from rock.deployments.config import DeploymentConfig
from rock.deployments.constants import Status
from rock.deployments.docker import DockerDeployment
from rock.deployments.status import ServiceStatus
from rock.logger import init_logger
from rock.sandbox.gem_actor import GemActor

logger = init_logger(__name__)


@ray.remote(num_cpus=0, scheduling_strategy="SPREAD")
class SandboxActor(GemActor):
    _export_interval_millis: int = 10000
    _stop_time: datetime.datetime = None
    _clean_container_background_script = "rock/admin/scripts/clean_container_background.sh"
    _clean_container_background_process = None
    _metrics_monitor = None
    _role = "test"
    _env = "dev"
    _user_id = "default"
    _experiment_id = "default"
    _namespace = "default"

    def __init__(
        self,
        config: DeploymentConfig,
        deployment: AbstractDeployment,
        clean_container_background_script_path: str | None = None,
    ):
        super().__init__(config=config, deployment=deployment)

        if clean_container_background_script_path:
            self._clean_container_background_script = clean_container_background_script_path
        self._role = config.role
        self._env = config.env

    def _clean_container_background(self):
        logger.info("run background subprocess for DockerDeployment")
        try:
            # Check if script file exists
            if not os.path.exists(self._clean_container_background_script):
                logger.info(f"Background script file does not exist: {self._clean_container_background_script}")
                return

            logger.info("start to run background script")
            actor_pid = str(os.getpid())
            logger.info(f"actor_pid is {actor_pid}")
            # Execute script
            process = subprocess.Popen(
                ["bash", self._clean_container_background_script, actor_pid, self._config.container_name],
            )
            logger.info(f"Background script started successfully, pid is {process.pid}")
            self._clean_container_background_process = process
        except Exception as e:
            logger.error(f"Error occurred while running background script: {e}", exc_info=True)

    async def _run_shell_command(
        self, *args: str, check: bool = True, timeout: float = None, env=None, input=None
    ) -> subprocess.CompletedProcess:
        process = None
        try:
            process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                ),
                timeout=timeout,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(input=input), timeout=timeout)

            if check and process.returncode != 0:
                logger.error(
                    f"run shell command {args} failed, return code {process.returncode}, stderr {stderr.decode().strip()}"
                )
                error_info = {
                    "exception_type": "CalledProcessError",
                    "returncode": process.returncode,
                    "cmd": args,
                    "stdout": stdout.decode(errors="ignore") if stdout else "",
                    "stderr": stderr.decode(errors="ignore") if stderr else "",
                }
                import json

                raise RuntimeError(json.dumps(error_info))

            return subprocess.CompletedProcess(args=args, returncode=process.returncode, stdout=stdout, stderr=stderr)
        except asyncio.TimeoutError:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            raise subprocess.TimeoutExpired(args, timeout)

    async def start(self):
        try:
            await self._deployment.start()
        except Exception as ex:
            logger.error(f"start deployment failed: {ex}", exc_info=True)
            raise ex
        if isinstance(self._deployment, DockerDeployment):
            self._clean_container_background()
        await self._setup_monitor()

    async def stop(self):
        logger.info("start to stop")
        try:
            await self._deployment.stop()
            logger.info("deployment stopped")
            self._clean_container_background_process.kill()
            logger.info("actor stopped")
        except Exception as e:
            logger.error(f"Error occurred while stopping container: {e}", exc_info=True)

    async def commit(self, image_tag: str, username: str, password: str) -> CommandResponse:
        logger.info(f"start to commit {self._config.container_name} to {image_tag}")
        with tempfile.TemporaryDirectory() as docker_config_dir:
            env = os.environ.copy()
            env["DOCKER_CONFIG"] = docker_config_dir
            repo_url = f"https://{image_tag}" if not image_tag.startswith("http") else image_tag
            from urllib.parse import urlparse

            parsed = urlparse(repo_url)

            await self._run_shell_command(
                "docker",
                "login",
                parsed.netloc,
                "--username",
                username,
                "--password-stdin",
                env=env,
                input=password.encode(),
            )
            logger.info(f"docker login {parsed.netloc} succeed")

            process = await self._run_shell_command("docker", "commit", self._config.container_name, image_tag)
            logger.info(
                f"commit container {self._config.container_name} to {image_tag} succeed, message {process.stdout.decode().strip()}"
            )

            process = await self._run_shell_command("docker", "push", image_tag, env=env)
            return CommandResponse(
                stdout=process.stdout.decode().strip(),
                stderr=process.stderr.decode().strip(),
                exit_code=process.returncode,
            )

    async def is_alive(self):
        try:
            return await self._deployment.is_alive()
        except Exception as e:
            logger.error(f"failed to check is alive: {e}", exc_info=True)
            return IsAliveResponse(is_alive=False)

    async def execute(self, command: Command) -> CommandResponse:
        await self._refresh_stop_time()
        return await self._deployment.runtime.execute(command)

    async def create_session(self, request: CreateBashSessionRequest) -> CreateBashSessionResponse:
        await self._refresh_stop_time()
        return await self._deployment.runtime.create_session(request)

    async def run_in_session(self, action: BashAction) -> BashObservation:
        await self._refresh_stop_time()
        return await self._deployment.runtime.run_in_session(action)

    async def close_session(self, request: CloseBashSessionRequest) -> CloseBashSessionResponse:
        await self._refresh_stop_time()
        return await self._deployment.runtime.close_session(request)

    async def read_file(self, request: ReadFileRequest) -> ReadFileResponse:
        await self._refresh_stop_time()
        return await self._deployment.runtime.read_file(request)

    async def write_file(self, request: WriteFileRequest) -> WriteFileResponse:
        await self._refresh_stop_time()
        return await self._deployment.runtime.write_file(request)

    async def upload(self, file: UploadFile, target_path: str) -> UploadResponse | None:
        await self._refresh_stop_time()
        if isinstance(self._deployment, DockerDeployment):
            return await self._deployment.runtime.async_upload(file, target_path)
        return None

    async def is_expired(self):
        return self._stop_time < datetime.datetime.now()

    async def host_name(self) -> str | None:
        if isinstance(self._deployment, DockerDeployment):
            return self._deployment.pod_name
        return None

    async def get_status(self) -> ServiceStatus:
        if isinstance(self._deployment, DockerDeployment):
            return self._deployment.get_status()
        else:
            status = ServiceStatus()
            status.update_status("image_pull", Status.FAILED, "not supported on current deployment")
            status.update_status("docker_run", Status.FAILED, "not supported on current deployment")
            return status

    async def host_ip(self) -> str | None:
        try:
            hostname = socket.gethostname()
            ip_address = socket.gethostbyname(hostname)
            return ip_address
        except socket.gaierror as e:
            print(f"Unable to resolve hostname: {e}")
            return None

    async def set_user_id(self, user_id: str):
        self._user_id = user_id

    async def set_experiment_id(self, experiment_id: str):
        self._experiment_id = experiment_id

    async def set_namespace(self, namespace: str):
        self._namespace = namespace

    async def user_id(self) -> str | None:
        if isinstance(self._deployment, DockerDeployment):
            return self._user_id
        return None

    async def experiment_id(self) -> str | None:
        if isinstance(self._deployment, DockerDeployment):
            return self._experiment_id
        return None

    async def namespace(self) -> str | None:
        if isinstance(self._deployment, DockerDeployment):
            return self._namespace
        return None

    async def sandbox_info(self) -> SandboxInfo:
        if isinstance(self._deployment, DockerDeployment):
            return {
                "host_ip": await self.host_ip(),
                "host_name": await self.host_name(),
                "image": self._config.image,
                "user_id": await self.user_id(),
                "experiment_id": await self.experiment_id(),
                "sandbox_id": self._config.container_name,
                "namespace": await self.namespace(),
                "cpus": self._config.cpus,
                "memory": self._config.memory,
            }
        return {}
