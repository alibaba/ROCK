from abc import ABC, abstractmethod

from rock.config import RuntimeConfig


class RuntimeEnv(ABC):
    """Abstract base class for different runtime environments.

    Each runtime environment defines how to mount volumes and start the rocklet
    within a container.
    """

    @abstractmethod
    def get_volume_mounts(self):
        """Get the volume mount configurations for this runtime environment.

        Returns:
            list: A list of volume mount configurations, each containing
                  'local' and 'container' paths.
        """
        pass

    @abstractmethod
    def get_rocklet_start_cmd(self):
        """Get the command to start the rocklet in the container.

        Returns:
            str: The command to execute to start the rocklet.
        """
        pass


class DockerRuntimeEnv(RuntimeEnv):
    """Docker runtime environment.

    This environment depends on having an executable /tmp/miniforge/bin/rocklet
    directly available in the current deployment environment. This requirement
    needs to be fulfilled by using a customized Docker image.
    """

    def get_volume_mounts(self):
        """Get volume mounts for Docker environment.

        Mounts:
        - /tmp/miniforge: Contains the pre-installed Python environment
        - /tmp/local_files: Contains local files needed for execution
        """
        mount_configs = [
            {
                "local": "/tmp/miniforge",
                "container": "/tmp/miniforge",
            },
            {
                "local": "/tmp/local_files",
                "container": "/tmp/local_files",
            },
        ]

        return mount_configs

    def get_rocklet_start_cmd(self):
        """Get the command to start rocklet in Docker environment.

        Makes the docker_run.sh script executable and executes it.
        """
        cmd = "cp /tmp/local_files/docker_run.sh /tmp/docker_run.sh && chmod +x /tmp/docker_run.sh && /tmp/docker_run.sh"
        return cmd


class LocalRuntimeEnv(RuntimeEnv):
    """Local runtime environment.

    This environment depends on being able to directly mount the current
    deployment's .venv and the corresponding base Python interpreter into
    the container. This generally requires the same operating system
    between the host and container.
    """

    def __init__(self, runtime_config: RuntimeConfig):
        super().__init__()
        self._runtime_config = runtime_config

    # TODO: rocklet -> runtime_env
    def get_volume_mounts(self):
        """Get volume mounts for local environment.

        Mounts:
        - python_env_path: The Python environment path
        - project_root: The project root directory
        - .venv: The virtual environment directory (mounted as /tmp/miniforge in container)
        - local_files: Local files needed for execution
        """
        project_root = self._runtime_config.project_root
        python_env_path = self._runtime_config.python_env_path
        mount_configs = [
            {
                "local": python_env_path,
                "container": python_env_path,
            },
            {
                "local": project_root,
                "container": project_root,
            },
            {
                "local": f"{project_root}/.venv",
                "container": "/tmp/miniforge",
            },
            {
                "local": f"{project_root}/rock/rocklet/local_files",
                "container": "/tmp/local_files",
            },
        ]

        return mount_configs

    def get_rocklet_start_cmd(self):
        """Get the command to start rocklet in local environment.

        Makes the docker_run.sh script executable and executes it.
        """
        cmd = "cp /tmp/local_files/docker_run.sh /tmp/docker_run.sh && chmod +x /tmp/docker_run.sh && /tmp/docker_run.sh"
        return cmd


class UvRuntimeEnv(RuntimeEnv):
    """UV runtime environment.

    This environment only depends on having the ROCK project available,
    which can be mounted into the container. However, initialization is
    slower and has higher network requirements. This is the recommended
    option for MacOS.
    """

    def __init__(self, runtime_config: RuntimeConfig):
        super().__init__()
        self._runtime_config = runtime_config

    def get_volume_mounts(self):
        """Get volume mounts for UV environment.

        Mounts:
        - project_root: The project root directory (mounted as /tmp + project_root in container)
        - local_files: Local files needed for execution
        """
        project_root = self._runtime_config.project_root
        mount_configs = [
            {
                "local": project_root,
                "container": f"/tmp{project_root}",
            },
            {
                "local": f"{project_root}/rock/rocklet/local_files",
                "container": "/tmp/local_files",
            },
        ]

        return mount_configs

    def get_rocklet_start_cmd(self):
        """Get the command to start rocklet in UV environment.

        Makes the docker_run_with_uv.sh script executable and executes it
        with the project root path as an argument.
        """

        container_project_root = f"/tmp{self._runtime_config.project_root}"
        cmd = (
            f"cp /tmp/local_files/docker_run_with_uv.sh /tmp/docker_run_with_uv.sh &&"
            f"chmod +x /tmp/docker_run_with_uv.sh && "
            f"/tmp/docker_run_with_uv.sh '{container_project_root}'"
        )
        return cmd


class PipRuntimeEnv(RuntimeEnv):
    def __init__(self, runtime_config: RuntimeConfig):
        super().__init__()
        self._runtime_config = runtime_config

    def get_volume_mounts(self):
        project_root = self._runtime_config.project_root
        mount_configs = [
            {
                "local": f"{project_root}/rock/rocklet/local_files",
                "container": "/tmp/local_files",
            },
        ]

        return mount_configs

    def get_rocklet_start_cmd(self):
        cmd = "cp /tmp/local_files/docker_run_with_pip.sh /tmp/docker_run_with_pip.sh && chmod +x /tmp/docker_run_with_pip.sh && /tmp/docker_run_with_pip.sh"
        return cmd
