from abc import ABC, abstractmethod


class AbstractDirStorage(ABC):
    """Directory-level archive storage.

    Caller passes a local directory path; the storage implementation
    decides compression format and upload strategy internally.
    """

    @abstractmethod
    async def upload_dir(self, local_dir: str, key: str) -> None:
        """Pack and upload local_dir to key. Raises FileNotFoundError if local_dir missing."""

    @abstractmethod
    async def download_to_dir(self, key: str, local_dir: str) -> None:
        """Download key and restore to local_dir.

        Raises FileNotFoundError if key missing, FileExistsError if local_dir exists.
        """

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete key. Returns False if not found."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if key exists."""


class AbstractImageStorage(ABC):
    """Container image archive storage.

    push_from_local / pull_to_local must run on the node owning the image
    (node-local docker daemon). exists / delete are registry HTTP API calls.
    """

    @property
    @abstractmethod
    def registry_url(self) -> str:
        """Registry endpoint (no scheme), e.g. 'localhost:5000'."""

    @abstractmethod
    async def push_from_local(self, local_image_tag: str, remote_image_ref: str) -> None:
        """Tag local image and push to registry."""

    @abstractmethod
    async def pull_to_local(self, remote_image_ref: str) -> None:
        """Pull image from registry to local daemon. Raises RuntimeError if not found."""

    @abstractmethod
    async def delete(self, image_ref: str) -> bool:
        """Delete manifest from registry via V2 HTTP API. Returns False if 404."""

    @abstractmethod
    async def exists(self, image_ref: str) -> bool:
        """HEAD manifest via V2 HTTP API."""
