from pathlib import Path

from rock.logger import init_logger
from rock.sdk.builder.base import EnvBuilder
from rock.sdk.builder.provider.docker import Docker
from rock.sdk.builder.provider.dockerfile_builder import DockerfileBuilder

logger = init_logger(__name__)


class TerminalBenchEnvBuilder(EnvBuilder):
    async def build(
        self,
        instance_record: dict[str, str] | None = None,
        registry_url: str = "",
        image_namespace: str = "",
        image_repo: str = "",
        username: str = "",
        password: str = "",
        **kwargs,
    ):
        if instance_record is None:
            raise Exception("instance_record is required")

        instance_id = instance_record.get("instance_id", None)
        build_dir = Path(__file__).parent / f"build_dir_{instance_record.get('__index_level_0__', '0')}/{instance_id}/"
        build_dir = str(build_dir)
        logger.info(f"Building sandbox image for {instance_id}, build_dir {build_dir}")
        image_repo_addr = f"{registry_url.rstrip('/')}/{image_namespace}/{image_repo}"
        image_tag = f"{image_repo_addr}:{instance_id}.0.0.1"
        await DockerfileBuilder.build_terminal_bench(instance_record=instance_record, build_dir=build_dir)
        docker_file = f"{build_dir}/Dockerfile"
        image_repo_url = registry_url
        user_name = username
        password = password
        docker_kwargs = {
            "tag": image_tag,
            "file": docker_file,
            "registry_url": image_repo_url,
            "context_path": f"{build_dir}/",
            "user_name": user_name,
            "password": password,
        }
        docker = Docker(**docker_kwargs)
        docker.hub_login()
        try:
            image_info = docker.manifest()
            logger.debug(f"{image_tag} image info {image_info}")
            logger.info(f"{image_tag} already exists")
            return
        except Exception:
            pass
        docker.build()
        docker.push()
        docker.remove()

    async def verify(self, **kwargs):
        """Verify environment."""
        raise NotImplementedError
