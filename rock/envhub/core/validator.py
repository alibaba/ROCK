from abc import ABC

from rock.utils.docker import DockerUtil


class Validator(ABC):
    pass


class DockerValidator(Validator):
    """Validator for Docker environment requirements."""

    def check_docker(self) -> bool:
        """Validate basic Docker requirements - checks if docker command is available."""
        return DockerUtil.is_docker_available()

    def check_image(self, image_name: str) -> bool:
        return DockerUtil.is_image_available(image_name)
