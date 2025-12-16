import logging
import subprocess

from rock.sdk.builder.base import EnvBuilder
from rock.utils import ImageUtil, retry_async

logger = logging.getLogger(__name__)


class ImageMirror(EnvBuilder):
    @retry_async(max_attempts=3)
    async def build(
        self,
        instance_record: dict[str, str] | None = None,
        target_registry: str = "",
        target_username: str = "",
        target_password: str = "",
        source_registry: str | None = None,
        source_username: str | None = None,
        source_password: str | None = None,
    ):
        if not instance_record:
            raise Exception("instance_record is required")
        image = instance_record["docker_image"]
        registry_url, other_part = await ImageUtil.parse_registry_and_others(image)
        parsed_namespace, parsed_name, parsed_tag = await ImageUtil.split_image_name(other_part)
        aliyun_other_part = f"{parsed_namespace}/{parsed_name}:{parsed_tag}"
        target_image_full_name = f"{target_registry}/{aliyun_other_part}"

        # check if target exist, if exist, skip
        # login target hub
        result = subprocess.run(
            [
                "docker",
                "login",
                "-u",
                target_username,
                "-p",
                target_password,
                target_registry,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise Exception(
                f"docker login target hub failed {target_registry}, username is {target_username}, password is {target_password} error:{result.stderr}"
            )
        # check if target exist
        result = subprocess.run(
            ["docker", "manifest", "inspect", target_image_full_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info(f"{target_image_full_name} already exists, skip")
            return
        else:
            logger.info(f"{target_image_full_name} not exists, start to mirror")

        # login if source_username is not None
        if source_username is not None:
            if source_password is None:
                raise Exception("source_password is required")
            if source_registry is None or registry_url != source_registry:
                raise Exception(f"image {image} is not in source registry {source_registry}")
            result = subprocess.run(
                [
                    "docker",
                    "login",
                    "-u",
                    source_username,
                    "-p",
                    source_password,
                    source_registry,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise Exception(f"docker login source hub failed, {result.stderr}")
            logger.info("docker login success")

        # pull
        result = subprocess.run(["docker", "pull", image], capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"docker pull failed, {result.stderr}")
        logger.info(f"docker pull {image} success")

        # tag and push
        result = subprocess.run(
            ["docker", "tag", image, target_image_full_name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise Exception(f"docker tag failed, {result.stderr}")
        result = subprocess.run(["docker", "push", target_image_full_name], capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"docker push failed, {result.stderr}")
        logger.info(f"docker push {target_image_full_name} success")

    async def verify(self, instance_record: dict[str, str] | None = None):
        pass

    async def get_build_remote_one_split_command(self, split_filename: str, **kwargs) -> str:
        target_registry = kwargs.get("target_registry")
        target_username = kwargs.get("target_username")
        target_password = kwargs.get("target_password")
        source_registry = kwargs.get("source_registry")
        source_username = kwargs.get("source_username")
        source_password = kwargs.get("source_password")
        command = f"rock image mirror -f {split_filename} --target-registry={target_registry} --target-username={target_username} --target-password={target_password}"
        if source_registry is not None:
            command = f"{command} --source-registry={source_registry}"
        if source_username is not None:
            command = f"{command} --source-username={source_username}"
        if source_password is not None:
            command = f"{command} --source-password={source_password}"
        return command

    async def get_env_build_image(self):
        return "rock-n-roll-registry.cn-hangzhou.cr.aliyuncs.com/rock/rock-env-builder:0.2.1a1"
