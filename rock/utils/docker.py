import logging
import subprocess

logger = logging.getLogger(__name__)


class DockerUtil:
    """Docker operation utilities"""

    @classmethod
    def is_docker_available(cls):
        try:
            result = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                result = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
                return result.returncode == 0
            return False
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    @classmethod
    def is_image_available(cls, image: str) -> bool:
        """Check if a Docker image is available locally"""
        try:
            subprocess.check_call(["docker", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except subprocess.CalledProcessError:
            return False

    @classmethod
    def pull_image(cls, image: str) -> bytes:
        """Pull a Docker image"""
        try:
            return subprocess.check_output(["docker", "pull", image], stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            # e.stderr contains the error message as bytes
            raise subprocess.CalledProcessError(e.returncode, e.cmd, e.output, e.stderr) from None

    @classmethod
    def remove_image(image: str) -> bytes:
        """Remove a Docker image"""
        return subprocess.check_output(["docker", "rmi", image], timeout=30)


class ImageUtil:
    """Docker image name utilities"""

    DEFAULT_NAMESPACE: str = "library"
    DEFAULT_TAG: str = "latest"

    @classmethod
    async def split_image_name(cls, image_name: str) -> tuple[str, str, str]:
        """Split image name into namespace, name, and tag"""
        if "/" in image_name:
            repo_namespace_name, name_and_tag = image_name.split("/", maxsplit=1)
        else:
            repo_namespace_name = cls.DEFAULT_NAMESPACE
            name_and_tag = image_name
        if ":" in name_and_tag:
            repo_name, tag = name_and_tag.split(":")
        else:
            repo_name = name_and_tag
            tag = cls.DEFAULT_TAG

        logger.debug(f"repo_namespace_name: {repo_namespace_name}, repo_name: {repo_name}, tag: {tag}")
        return repo_namespace_name, repo_name, tag

    @classmethod
    async def parse_registry_and_others(cls, image_name: str) -> tuple[str, str]:
        parts = image_name.split("/", maxsplit=1)
        if len(parts) == 1:
            return "", image_name
        if "." in parts[0] or ":" in parts[0]:
            return parts[0], parts[1]
        else:
            return "", image_name
