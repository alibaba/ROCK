from abc import ABC, abstractmethod

from rock.utils.docker import DockerUtil


class SandboxValidator(ABC):
    """Base validator for sandbox environment requirements."""

    @abstractmethod
    def check_availability(self) -> bool:
        """Check if the underlying system is available."""
        pass

    @abstractmethod
    def check_resource(self, resource_id: str) -> bool:
        """Check if a specific resource is available."""
        pass


class DockerSandboxValidator(SandboxValidator):
    def check_availability(self) -> bool:
        """Check if Docker daemon is available."""
        return DockerUtil.is_docker_available()

    def check_resource(self, resource_id: str) -> bool:
        """Check if Docker image is available."""
        return DockerUtil.is_image_available(image=resource_id)
