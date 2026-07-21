import json

import ray

from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.admin.core.ray_service import RayService
from rock.common.constants import StopReason
from rock.config import RuntimeConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.docker import DockerDeployment
from rock.logger import init_logger
from rock.sandbox.operator.abstract import AbstractOperator
from rock.sandbox.sandbox_actor import SandboxActor
from rock.sandbox.utils.rocklet_probe import check_alive_status, get_remote_status
from rock.sdk.common.exceptions import BadRequestRockError
from rock.utils.format import parse_size_to_bytes
from rock.utils.service import build_sandbox_from_redis

logger = init_logger(__name__)


class RayOperator(AbstractOperator):
    def __init__(self, ray_service: RayService, runtime_config: RuntimeConfig):
        self._ray_service = ray_service
        self._runtime_config = runtime_config
        self._disk_scheduling_enabled = ray.is_initialized() and "disk" in ray.cluster_resources()

    def _get_actor_name(self, sandbox_id: str) -> str:
        return f"sandbox-{sandbox_id}"

    async def create_actor(
        self,
        config: DockerDeploymentConfig,
        pin_to_host_ip: str | None = None,
    ):
        actor_options = self._generate_actor_options(config, pin_to_host_ip=pin_to_host_ip)
        deployment: DockerDeployment = config.get_deployment()
        sandbox_actor = SandboxActor.options(**actor_options).remote(config, deployment)
        return sandbox_actor

    def _generate_actor_options(
        self,
        config: DockerDeploymentConfig,
        pin_to_host_ip: str | None = None,
    ) -> dict:
        actor_name = self._get_actor_name(config.container_name)
        actor_options = {"name": actor_name, "lifetime": "detached"}
        try:
            memory = parse_size_to_bytes(config.memory)
            actor_options["num_cpus"] = config.cpus
            actor_options["memory"] = memory
            custom_resources: dict[str, float] = {}
            if config.disk is not None and self._disk_scheduling_enabled:
                disk_bytes = parse_size_to_bytes(config.disk)
                if config.disk_overcommit_ratio and config.disk_overcommit_ratio > 1.0:
                    disk_bytes = int(disk_bytes / config.disk_overcommit_ratio)
                custom_resources["disk"] = disk_bytes
            # Pin to a specific node via Ray's implicit `node:<ip>` resource
            # (registered automatically per node with value 1.0; we consume a
            # negligible 0.001 so we don't block other actors). Used by restart
            # to land on the host that owns the existing container.
            if pin_to_host_ip:
                custom_resources[f"node:{pin_to_host_ip}"] = 0.001
            if custom_resources:
                actor_options["resources"] = custom_resources
            return actor_options
        except ValueError as e:
            logger.warning(f"Invalid memory size: {config.memory}", exc_info=e)
            raise BadRequestRockError(f"Invalid memory size: {config.memory}")

    async def submit(self, config: DockerDeploymentConfig, user_info: dict = {}) -> SandboxInfo:
        async with self._ray_service.get_ray_rwlock().read_lock():
            sandbox_id = config.container_name
            logger.info(f"[{sandbox_id}] start_async params:{json.dumps(config.model_dump(), indent=2)}")
            sandbox_actor: SandboxActor = await self.create_actor(config)
            sandbox_actor.set_metrics_endpoint.remote(self._runtime_config.metrics_endpoint)
            sandbox_actor.set_user_defined_tags.remote(self._runtime_config.user_defined_tags)
            sandbox_actor.start.remote()
            user_id = user_info.get("user_id", "default")
            experiment_id = user_info.get("experiment_id", "default")
            namespace = user_info.get("namespace", "default")
            rock_authorization = user_info.get("rock_authorization", "default")
            sandbox_actor.set_user_id.remote(user_id)
            sandbox_actor.set_experiment_id.remote(experiment_id)
            sandbox_actor.set_namespace.remote(namespace)
            sandbox_info: SandboxInfo = await self._ray_service.async_ray_get(sandbox_actor.sandbox_info.remote())
            sandbox_info["user_id"] = user_id
            sandbox_info["experiment_id"] = experiment_id
            sandbox_info["namespace"] = namespace
            sandbox_info["state"] = State.PENDING
            sandbox_info["rock_authorization"] = rock_authorization
            logger.info(f"sandbox {sandbox_id} is submitted")
            return sandbox_info

    async def get_remote_status(self, sandbox_id: str, host_ip: str):
        return await get_remote_status(sandbox_id, host_ip)

    async def get_status(self, sandbox_id: str) -> SandboxInfo | None:
        sandbox_info: SandboxInfo = await build_sandbox_from_redis(self._redis_provider, sandbox_id)
        if sandbox_info is None:
            return None
        host_ip = sandbox_info.get("host_ip")
        remote_status = await get_remote_status(sandbox_id, host_ip)
        is_alive = await check_alive_status(sandbox_id, host_ip, remote_status)
        # TODO: sink update state according to is_alive logic into SandboxInfo
        was_pending = sandbox_info.get("state") == State.PENDING
        if is_alive:
            sandbox_info["state"] = State.RUNNING
            if was_pending:
                # In submit(), sandbox_info.remote() races with start.remote() on async
                # actors, so the initial Redis write captures the pre-startup disk value.
                # When the rocklet is responding, start() is guaranteed complete; query
                # the actor to get the effective disk (None when storage-opt unsupported).
                # on_alive will persist this value to Redis so subsequent calls skip here.
                try:
                    actor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
                    actor_info = await self._ray_service.async_ray_get(actor.sandbox_info.remote(), timeout=5)
                    sandbox_info["disk"] = actor_info.get("disk")
                except Exception:
                    pass
        sandbox_info.update(remote_status.to_dict())
        return sandbox_info

    async def stop(self, sandbox_id: str, reason: StopReason = StopReason.MANUAL) -> bool:
        async with self._ray_service.get_ray_rwlock().read_lock():
            actor: SandboxActor = await self._ray_service.async_ray_get_actor(self._get_actor_name(sandbox_id))
            await self._ray_service.async_ray_get(actor.stop.remote(reason))
            logger.info(f"run time stop over {sandbox_id}")
            ray.kill(actor)
            return True

    async def delete(self, config: DockerDeploymentConfig, host_ip: str | None = None) -> bool:
        async with self._ray_service.get_ray_rwlock().read_lock():
            sandbox_id = config.container_name
            actor_name = self._get_actor_name(sandbox_id)

            try:
                existing_actor = await self._ray_service.async_ray_get_actor(actor_name)
                ray.kill(existing_actor)
            except Exception:
                logger.info(f"Actor {actor_name} already gone, proceeding with delete")

            if not host_ip:
                logger.warning(
                    f"delete for {sandbox_id} called without host_ip; new actor "
                    f"may be scheduled on a node that does not own the container"
                )
            config.cpus = 0.01
            config.memory = "128m"
            sandbox_actor: SandboxActor = await self.create_actor(config, pin_to_host_ip=host_ip)
            try:
                await self._ray_service.async_ray_get(sandbox_actor.delete.remote())
                logger.info(f"sandbox {sandbox_id} deleted on host_ip={host_ip}")
                return True
            finally:
                ray.kill(sandbox_actor)

    async def restart(self, config: DockerDeploymentConfig, host_ip: str | None = None) -> SandboxInfo:
        """Restart an existing sandbox using docker start (container is preserved).

        Flow:
          1. Create a fresh detached actor pinned to ``host_ip`` (the node that
             owns the existing container) — without this Ray would schedule the
             new actor on any free node and `docker inspect <container>` would
             fail with "container does not exist" on that wrong node.
          2. actor.restart() → deployment.restart() — DockerDeployment.restart()
             handles a still-running container by issuing `docker kill` first.
        """
        async with self._ray_service.get_ray_rwlock().read_lock():
            sandbox_id = config.container_name

            if not host_ip:
                logger.warning(
                    f"restart for {sandbox_id} called without host_ip; new actor "
                    f"may be scheduled on a node that does not own the container"
                )
            sandbox_actor: SandboxActor = await self.create_actor(config, pin_to_host_ip=host_ip)
            await self._ray_service.async_ray_get(sandbox_actor.restart.remote())

            sandbox_info: SandboxInfo = await self._ray_service.async_ray_get(sandbox_actor.sandbox_info.remote())
            sandbox_info["state"] = State.PENDING
            logger.info(f"sandbox {sandbox_id} restarted")
            return sandbox_info

    async def start_archive(
        self,
        config: DockerDeploymentConfig,
        host_ip: str | None,
        dir_storage_config: dict,
        image_storage_config: dict,
        archive_params: dict | None = None,
    ) -> None:
        async with self._ray_service.get_ray_rwlock().read_lock():
            config.cpus = 0.1
            config.memory = "256m"
            sandbox_actor = await self.create_actor(config, pin_to_host_ip=host_ip)
            sandbox_actor.archive.remote(dir_storage_config, image_storage_config, archive_params)

    async def start_restore(
        self,
        config: DockerDeploymentConfig,
        dir_storage_config: dict,
        image_storage_config: dict,
        archive_params: dict | None = None,
    ) -> str | None:
        """Fire-and-forget restore: actor pulls image, downloads logs, starts container.

        The actor stays alive as the long-lived detached actor for the sandbox
        (same as what ``submit`` creates for a new sandbox).  ``get_status``
        alive detection will transition the state to RUNNING once the container
        is up.

        Returns the host IP of the node where the actor was scheduled so the
        caller can persist it to the meta store (needed for ``get_status`` to
        reach the correct worker).
        """
        async with self._ray_service.get_ray_rwlock().read_lock():
            sandbox_actor = await self.create_actor(config)
            new_host_ip = await self._ray_service.async_ray_get(sandbox_actor.host_ip.remote())
            sandbox_actor.restore_and_start.remote(dir_storage_config, image_storage_config, archive_params)
            return new_host_ip
