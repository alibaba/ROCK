import json
import logging
import subprocess

logger = logging.getLogger(__name__)


class DockerUtil:
    """Docker operation utilities"""

    @classmethod
    def get_docker_info(cls) -> dict | None:
        """Run 'docker info' and return the parsed JSON, or None on failure."""
        try:
            result = subprocess.run(
                ["docker", "info", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning("get_docker_info: docker info failed")
                return None
            return json.loads(result.stdout)
        except Exception as e:
            logger.warning(f"get_docker_info: failed: {e}")
            return None

    @classmethod
    def get_docker_root_dir(cls) -> str | None:
        """Return DockerRootDir from docker info, or None on failure."""
        info = cls.get_docker_info()
        if info is None:
            return None
        root = info.get("DockerRootDir")
        if not root:
            logger.warning("get_docker_root_dir: DockerRootDir not found in docker info")
        return root or None

    @classmethod
    def is_xfs_prjquota_path(cls, path: str) -> bool:
        """Return True if *path* is on an XFS mount with prjquota (or pquota) enabled.

        This is the prerequisite for XFS project quota on a directory.
        Unlike detect_storage_opt_support(), this check is independent of Docker's
        storage driver configuration.
        """
        try:
            result = subprocess.run(
                ["findmnt", "-T", path, "-o", "FSTYPE,OPTIONS", "--noheadings"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.info(f"is_xfs_prjquota_path: findmnt failed for {path!r}")
                return False
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if not lines:
                logger.info(f"is_xfs_prjquota_path: empty findmnt output for {path!r}")
                return False
            # the last line is the most recent mount point
            line = lines[-1]
            parts = line.split(None, 1)  # split on first whitespace: FSTYPE  OPTIONS
            if len(parts) < 2:
                logger.info(f"is_xfs_prjquota_path: unexpected findmnt output for {path!r}: {line!r}")
                return False
            fstype, options = parts[0], parts[1]
            if fstype != "xfs":
                logger.info(f"is_xfs_prjquota_path: {path!r} is on {fstype!r}, not xfs")
                return False
            opts = options.split(",")
            has_prjquota = "prjquota" in opts or "pquota" in opts
            if not has_prjquota:
                logger.info(
                    f"is_xfs_prjquota_path: {path!r} is xfs but mount options {options!r} missing prjquota/pquota"
                )
            return has_prjquota
        except Exception as e:
            logger.info(f"is_xfs_prjquota_path: findmnt command failed for {path!r}: {e}")
            return False

    @classmethod
    def detect_storage_opt_support(cls) -> bool:
        """Detect whether rootfs disk-limit can be enforced in this environment.

        Supported configurations:
        1. overlay2 storage driver + XFS with prjquota  (native ``--storage-opt size=``)
        2. containerd image store (snapshotter) + XFS with prjquota  (XFS quota fallback;
           ``--storage-opt`` is silently ignored by containerd, quota is applied manually)

        Returns:
            True if rootfs disk-limit can be enforced, False otherwise.
        """
        info = cls.get_docker_info()
        if info is None:
            return False

        docker_root = info.get("DockerRootDir")
        if not docker_root:
            logger.warning("detect_storage_opt_support: DockerRootDir not found in docker info")
            return False

        driver = info.get("Driver", "")

        # Path 1: overlay2 — native --storage-opt size=
        if driver == "overlay2":
            if not cls.is_xfs_prjquota_path(docker_root):
                return False
            logger.info(f"detect_storage_opt_support: supported — overlay2, {docker_root!r} is xfs+prjquota")
            return True

        # Path 2: containerd image store — XFS quota fallback
        if cls.detect_containerd_image_store(info):
            if not cls.is_xfs_prjquota_path(docker_root):
                return False
            logger.info(
                f"detect_storage_opt_support: supported — containerd image store, "
                f"{docker_root!r} is xfs+prjquota (will use XFS quota fallback)"
            )
            return True

        logger.info(f"detect_storage_opt_support: storage driver is {driver!r}, not overlay2 or containerd snapshotter")
        return False

    @classmethod
    def detect_containerd_image_store(cls, info: dict | None = None) -> bool:
        """Return True when Docker is using the containerd image store (snapshotter).

        Detection is based on the ``driver-type`` entry inside ``DriverStatus``
        from ``docker info``.  When the containerd image store is enabled the
        driver-type is ``io.containerd.snapshotter.v1`` regardless of which
        snapshotter (overlayfs, native, btrfs …) is active.
        """
        if info is None:
            info = cls.get_docker_info()
        if info is None:
            return False
        for entry in info.get("DriverStatus") or []:
            if isinstance(entry, list) and len(entry) == 2 and entry[0] == "driver-type":
                return entry[1] == "io.containerd.snapshotter.v1"
        return False

    @classmethod
    def is_docker_available(cls):
        try:
            result = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                result = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=30)
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
    def login(cls, registry: str, username: str, password: str, timeout: int = 30) -> str:
        """Login to a Docker registry

        Args:
            registry: Docker registry URL (e.g. registry.example.com)
            username: Registry username
            password: Registry password
            timeout: Command timeout in seconds

        Returns:
            Command output as string on success

        Raises:
            subprocess.CalledProcessError: If login fails
        """
        try:
            result = subprocess.run(
                ["docker", "login", registry, "-u", username, "--password-stdin"],
                input=password,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                logger.error(f"Docker login to {registry} failed: {result.stderr.strip()}")
                raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
            logger.info(f"Successfully logged in to {registry}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error(f"Docker login to {registry} timed out after {timeout}s")
            raise

    @classmethod
    def logout(cls, registry: str, timeout: int = 30) -> str:
        """Logout from a Docker registry

        Args:
            registry: Docker registry URL (e.g. registry.example.com)
            timeout: Command timeout in seconds

        Returns:
            Command output as string on success

        Raises:
            subprocess.CalledProcessError: If logout fails
        """
        try:
            result = subprocess.run(
                ["docker", "logout", registry],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                logger.error(f"Docker logout from {registry} failed: {result.stderr.strip()}")
                raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
            logger.info(f"Successfully logged out from {registry}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error(f"Docker logout from {registry} timed out after {timeout}s")
            raise

    @classmethod
    def remove_image(cls, image: str) -> bytes:
        """Remove a Docker image"""
        return subprocess.check_output(["docker", "rmi", image], timeout=30)

    @classmethod
    def remove_container_force(cls, name: str, timeout: int = 10) -> None:
        """Swallows errors (missing container, daemon hiccups, timeout) and only
        warns — intended for cleanup paths where the caller's primary error
        must still propagate.
        """
        try:
            subprocess.run(
                ["docker", "rm", "-f", "-v", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        except Exception as e:
            logger.warning(f"Remove of container {name} failed: {e}")


class ImageUtil:
    """Docker image name utilities"""

    DEFAULT_NAMESPACE: str = "library"
    DEFAULT_TAG: str = "latest"

    @classmethod
    def split_image_name(cls, image_name: str) -> tuple[str, str, str]:
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
    def parse_registry_and_others(cls, image_name: str) -> tuple[str, str]:
        parts = image_name.split("/", maxsplit=1)
        if len(parts) == 1:
            return "", image_name
        if "." in parts[0] or ":" in parts[0]:
            return parts[0], parts[1]
        else:
            return "", image_name
