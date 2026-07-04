import asyncio
import datetime
import os
import shutil
import socket
import subprocess
import tempfile

import ray
from fastapi import UploadFile

from rock import env_vars
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
from rock.common.constants import StopReason
from rock.config import ArchiveDirStorageConfig
from rock.deployments.abstract import AbstractDeployment
from rock.deployments.config import DeploymentConfig
from rock.deployments.constants import Status
from rock.deployments.docker import DockerDeployment
from rock.deployments.status import PersistedServiceStatus, PhaseStatus, ServiceStatus
from rock.logger import init_logger
from rock.sandbox.archive.abstract import AbstractDirStorage
from rock.sandbox.archive.constants import ArchiveKeys
from rock.sandbox.archive.registry_v2 import DockerRegistryV2ImageStorage
from rock.sandbox.gem_actor import GemActor
from rock.utils.format import parse_size_to_bytes

logger = init_logger(__name__)


@ray.remote(scheduling_strategy="SPREAD")
class SandboxActor(GemActor):
    _export_interval_millis: int = 10000
    _stop_time: datetime.datetime = None
    _clean_container_background_script = "rock/admin/scripts/clean_container_background.sh"
    _clean_container_background_process = None
    _metrics_monitor = None
    _archive_status = None
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
            # start_new_session: detach from actor's session so SIGHUP doesn't
            # take the watchdog down (replaces the script's prior `nohup ... &`
            # form, which made the watchdog uncontrollable from Python).
            process = subprocess.Popen(
                ["bash", self._clean_container_background_script, actor_pid, self._config.container_name],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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
        except asyncio.CancelledError:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            raise

    async def _get_image_size(self, image_tag: str) -> int:
        result = await self._run_shell_command("docker", "image", "inspect", "--format={{.Size}}", image_tag)
        return int(result.stdout.decode().strip())

    async def _get_dir_size(self, dir_path: str) -> int:
        result = await self._run_shell_command("du", "-sb", dir_path)
        return int(result.stdout.decode().split()[0])

    async def start(self):
        try:
            await self._deployment.start()
        except Exception as ex:
            logger.error(f"[{self._config.container_name}] start deployment failed: {ex}", exc_info=True)
            raise ex
        if isinstance(self._deployment, DockerDeployment):
            self._config.disk = self._deployment.effective_disk
            self._clean_container_background()
        await self._setup_monitor()

    async def stop(self, reason: StopReason = StopReason.MANUAL):
        logger.info(f"[{self._config.container_name}] start to stop (reason={reason.value})")
        try:
            await self._deployment.stop()
            logger.info(f"[{self._config.container_name}] deployment stopped")
            self._clean_container_background_process.kill()
            logger.info(f"[{self._config.container_name}] actor stopped")
        except Exception as e:
            logger.error(f"[{self._config.container_name}] Error occurred while stopping container: {e}", exc_info=True)
        finally:
            self.log_lifecycle_summary(reason)

    async def restart(self):
        """Restart an existing stopped container using docker start.

        On success the actor stays alive as the long-lived runtime for the
        RUNNING sandbox.  On any failure the actor exits so a stale detached
        actor doesn't linger — the manager-side reconcile loop will observe
        the sandbox stuck in PENDING and stop it back to STOPPED.
        """
        logger.info(f"[{self._config.container_name}] start to restart")
        try:
            await self._deployment.restart()
            logger.info(f"[{self._config.container_name}] deployment restarted")

            # Re-arm the cleanup watchdog so a future actor death still triggers
            # docker stop — start() does this too; restart must stay symmetric.
            if isinstance(self._deployment, DockerDeployment):
                self._clean_container_background()

            # Re-establish monitoring after restart
            await self._setup_monitor()
            logger.info(f"[{self._config.container_name}] actor restarted")
        except Exception as e:
            logger.exception(f"[{self._config.container_name}] restart failed: {e}")
            ray.actor.exit_actor()

    async def delete(self):
        container_name = self._config.container_name if self._config else None
        logger.info(f"[{container_name}] start to delete")
        try:
            await self._deployment.delete()
            logger.info(f"[{container_name}] deployment deleted")
        except Exception as e:
            logger.error(f"[{container_name}] Error occurred while deleting container: {e}", exc_info=True)

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
            logger.error(f"[{self._config.container_name}] failed to check is alive: {e}", exc_info=True)
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
                "disk": self._config.disk,
            }
        return {}

    async def archive(
        self,
        dir_storage_config: dict,
        image_storage_config: dict,
        archive_params: dict | None = None,
    ) -> None:
        """Fire-and-forget archive: commit+push image, upload logs, then self-exit."""
        archive_params = archive_params or {}
        timeout = archive_params.get("timeout_seconds", 1800)
        try:
            await asyncio.wait_for(
                self._do_archive(dir_storage_config, image_storage_config, archive_params),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error(f"[{self._config.container_name}] archive timed out after {timeout}s")
            if self._archive_status:
                self._archive_status.update_status("image_archive", Status.TIMEOUT, f"timeout after {timeout}s")
        except Exception as e:
            logger.exception(f"[{self._config.container_name}] archive failed: {e}")
            if self._archive_status:
                self._archive_status.update_status("image_archive", Status.FAILED, str(e)[:200])
        finally:
            ray.actor.exit_actor()

    async def _do_archive(
        self,
        dir_storage_config: dict,
        image_storage_config: dict,
        archive_params: dict,
    ) -> None:
        sandbox_id = self._config.container_name
        dir_storage = AbstractDirStorage.from_config(ArchiveDirStorageConfig(**dir_storage_config))
        image_storage = DockerRegistryV2ImageStorage(**image_storage_config)
        prefix = archive_params.get("archive_prefix", "rock-archives/")
        registry_ns = archive_params.get("registry_namespace", "sandbox_archive")

        self._archive_status = PersistedServiceStatus(
            phases={"image_archive": PhaseStatus(), "log_archive": PhaseStatus()}
        )
        self._archive_status.set_sandbox_id(sandbox_id)
        self._archive_status.update_status("image_archive", Status.RUNNING, "committing container")

        local_tag = f"archive-staging-{sandbox_id}:latest"
        await self._run_shell_command("docker", "commit", sandbox_id, local_tag)

        log_root = env_vars.ROCK_LOGGING_PATH
        log_dir = f"{log_root}/{sandbox_id}" if log_root else ""
        has_log_dir = bool(log_dir) and os.path.isdir(log_dir)

        # Pre-flight size checks
        max_image = archive_params.get("max_image_push_size", "")
        if max_image:
            image_size = await self._get_image_size(local_tag)
            max_bytes = parse_size_to_bytes(max_image)
            if image_size > max_bytes:
                await self._run_shell_command("docker", "rmi", local_tag, check=False)
                raise RuntimeError(
                    f"[{sandbox_id}] image size {image_size} bytes exceeds limit {max_image} ({max_bytes} bytes)"
                )

        max_dir = archive_params.get("max_dir_upload_size", "")
        if max_dir and has_log_dir:
            dir_size = await self._get_dir_size(log_dir)
            max_bytes = parse_size_to_bytes(max_dir)
            if dir_size > max_bytes:
                await self._run_shell_command("docker", "rmi", local_tag, check=False)
                raise RuntimeError(
                    f"[{sandbox_id}] log dir size {dir_size} bytes exceeds limit {max_dir} ({max_bytes} bytes)"
                )

        # Upload logs (dir first, then image)
        dir_uploaded = False
        if has_log_dir:
            key = ArchiveKeys.dir_key(sandbox_id, prefix)
            self._archive_status.update_status("log_archive", Status.RUNNING, "uploading logs")
            try:
                await dir_storage.upload_dir(log_dir, key)
                dir_uploaded = True
                self._archive_status.update_status("log_archive", Status.SUCCESS, "logs uploaded")
            except Exception:
                self._archive_status.update_status("log_archive", Status.FAILED, "upload failed")
                await self._run_shell_command("docker", "rmi", local_tag, check=False)
                raise
            shutil.rmtree(log_dir, ignore_errors=True)
        else:
            self._archive_status.update_status("log_archive", Status.SUCCESS, "skipped")

        ref = ArchiveKeys.image_ref(sandbox_id, image_storage.registry_url, registry_ns)
        # Delete existing tag before push — some registries (ACR) reject overwrites.
        # Swallow the error so a benign 404 (tag never existed) doesn't abort the archive,
        # but log at warning so a real auth/network failure is not silently masked.
        try:
            await image_storage.delete(ref)
        except Exception as e:
            logger.warning(f"[{sandbox_id}] pre-push delete of {ref} failed (may be first archive): {e}")
        self._archive_status.update_status("image_archive", Status.RUNNING, "pushing to registry")
        try:
            await image_storage.push_from_local(local_tag, ref)
        except Exception:
            self._archive_status.update_status("image_archive", Status.FAILED, "push failed")
            await self._run_shell_command("docker", "rmi", local_tag, check=False)
            if dir_uploaded:
                try:
                    await dir_storage.delete(key)
                except Exception:
                    logger.warning(f"[{sandbox_id}] failed to cleanup dir {key} after image push failure")
            raise
        await self._run_shell_command("docker", "rmi", local_tag, check=False)
        self._archive_status.update_status("image_archive", Status.SUCCESS, "image archived")

        await self._deployment.delete()
        logger.info(f"[{sandbox_id}] archive complete")

    async def restore_and_start(
        self,
        dir_storage_config: dict,
        image_storage_config: dict,
        archive_params: dict | None = None,
    ) -> None:
        """Full restore: pull image + download logs + docker start + arm watchdog.

        On success the actor stays alive as the long-lived runtime for the
        RUNNING sandbox.  On any failure (timeout or exception) the actor
        exits so a stale detached actor doesn't linger — the manager-side
        reconcile loop will observe the missing actor and move the sandbox
        back to ARCHIVED via ``restore_failed``.
        """
        archive_params = archive_params or {}
        timeout = archive_params.get("timeout_seconds", 1800)
        try:
            await asyncio.wait_for(
                self._do_restore_and_start(dir_storage_config, image_storage_config, archive_params),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error(f"[{self._config.container_name}] restore timed out after {timeout}s")
            ray.actor.exit_actor()
        except Exception as e:
            logger.exception(f"[{self._config.container_name}] restore failed: {e}")
            ray.actor.exit_actor()

    async def _do_restore_and_start(
        self,
        dir_storage_config: dict,
        image_storage_config: dict,
        archive_params: dict,
    ) -> None:
        sandbox_id = self._config.container_name
        dir_storage = AbstractDirStorage.from_config(ArchiveDirStorageConfig(**dir_storage_config))
        image_storage = DockerRegistryV2ImageStorage(**image_storage_config)
        prefix = archive_params.get("archive_prefix", "rock-archives/")
        registry_ns = archive_params.get("registry_namespace", "sandbox_archive")

        ref = ArchiveKeys.image_ref(sandbox_id, image_storage.registry_url, registry_ns)
        await image_storage.pull_to_local(ref)

        log_root = env_vars.ROCK_LOGGING_PATH
        if log_root:
            key = ArchiveKeys.dir_key(sandbox_id, prefix)
            target_dir = f"{log_root}/{sandbox_id}"
            if await dir_storage.exists(key):
                try:
                    if os.path.exists(target_dir):
                        shutil.rmtree(target_dir)
                    await dir_storage.download_to_dir(key, target_dir)
                except Exception as e:
                    logger.warning(f"[{sandbox_id}] log restore failed, continuing without logs: {e}")
            else:
                logger.warning(f"[{sandbox_id}] archived log key {key} not found in OSS, skipping log restore")
        else:
            logger.info(f"[{sandbox_id}] ROCK_LOGGING_PATH not set, skipping log restore")

        await self._deployment.restart_from_image(ref)
        self._clean_container_background()
        await self._setup_monitor()
        logger.info(f"[{sandbox_id}] restore_and_start complete")
