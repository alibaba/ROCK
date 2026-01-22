import logging
import shlex
import tarfile
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path

from rock.actions import CreateBashSessionRequest, Observation
from rock.actions.sandbox.base import AbstractSandbox
from rock.actions.sandbox.request import ChmodRequest, ChownRequest, Command
from rock.actions.sandbox.response import ChmodResponse, ChownResponse, CommandResponse

logger = logging.getLogger(__name__)


class FileSystem(ABC):
    sandbox: AbstractSandbox = None

    def __init__(self, sandbox: AbstractSandbox = None):
        self.sandbox = sandbox

    @abstractmethod
    async def chown(self, request: ChownRequest) -> ChownResponse:
        pass

    @abstractmethod
    async def chmod(self, request: ChmodRequest) -> ChmodResponse:
        pass

    @abstractmethod
    async def upload_dir(
        self,
        source_dir: str | Path,
        target_dir: str,
        extract_timeout: int = 600,
    ) -> Observation:
        pass


class LinuxFileSystem(FileSystem):
    def __init__(self, sandbox: AbstractSandbox = None):
        super().__init__(sandbox)

    async def chown(self, request: ChownRequest) -> ChownResponse:
        paths = request.paths
        if paths is None or len(paths) == 0:
            raise Exception("paths is empty")

        command = ["chown"]
        if request.recursive:
            command.append("-R")
        command.extend([f"{request.remote_user}:{request.remote_user}"] + paths)
        logger.info(f"chown command: {command}")

        chown_response: CommandResponse = await self.sandbox.execute(Command(command=command))
        if chown_response.exit_code != 0:
            return ChownResponse(success=False, message=str(chown_response))
        return ChownResponse(success=True, message=str(chown_response))

    async def chmod(self, request: ChmodRequest) -> ChmodResponse:
        paths = request.paths
        if paths is None or len(paths) == 0:
            raise Exception("paths is empty")

        command = ["chmod"]
        if request.recursive:
            command.append("-R")

        command.extend([request.mode] + paths)
        logger.info(f"chmod command: {command}")
        chmod_response: CommandResponse = await self.sandbox.execute(Command(command=command))
        if chmod_response.exit_code != 0:
            return ChmodResponse(success=False, message=str(chmod_response))
        return ChmodResponse(success=True, message=str(chmod_response))

    async def upload_dir(
        self,
        source_dir: str | Path,
        target_dir: str,
        extract_timeout: int = 600,
    ) -> Observation:
        """Upload local directory to sandbox using tar.gz (simple version).

        - Create a random bash session internally
        - Check 'tar' exists; if not, return Observation with exit_code != 0
        - Pack source_dir fully into a tar.gz locally
        - Upload to sandbox /tmp
        - Extract into target_dir
        - Always cleanup local tar.gz

        Returns:
            Observation(exit_code=0) on success, otherwise exit_code!=0 with failure_reason.
        """
        local_tar_path: Path | None = None
        remote_tar_path: str | None = None
        session: str | None = None

        try:
            src = Path(source_dir).expanduser().resolve()
            if not src.exists():
                return Observation(exit_code=1, failure_reason=f"source_dir not found: {src}")
            if not src.is_dir():
                return Observation(exit_code=1, failure_reason=f"source_dir must be a directory: {src}")
            if not isinstance(target_dir, str) or not target_dir.startswith("/"):
                return Observation(exit_code=1, failure_reason=f"target_dir must be absolute path: {target_dir}")

            ts = str(time.time_ns())
            local_tar_path = Path(tempfile.gettempdir()) / f"rock_upload_{ts}.tar.gz"
            remote_tar_path = f"/tmp/rock_upload_{ts}.tar.gz"
            session = f"bash-{ts}"

            # create bash session
            await self.sandbox.create_session(CreateBashSessionRequest(session=session))

            # check tar exists
            check = await self.sandbox.arun(
                cmd="command -v tar >/dev/null 2>&1",
                session=session,
            )
            if check.exit_code != 0:
                return Observation(exit_code=1, failure_reason="sandbox has no tar command; cannot extract tarball")

            # pack locally
            try:
                with tarfile.open(local_tar_path, "w:gz") as tf:
                    tf.add(str(src), arcname=".")
            except Exception as e:
                raise Exception(f"tar pack failed: {e}")

            # upload tarball
            upload_response = await self.sandbox.upload_by_path(
                file_path=str(local_tar_path), target_path=remote_tar_path
            )
            if not upload_response.success:
                return Observation(exit_code=1, failure_reason=f"tar upload failed: {upload_response.message}")

            # extract
            extract_cmd = (
                f"rm -rf {shlex.quote(target_dir)} && mkdir -p {shlex.quote(target_dir)} && "
                f"tar -xzf {shlex.quote(remote_tar_path)} -C {shlex.quote(target_dir)}"
            )
            from rock.sdk.sandbox.client import RunMode

            res = await self.sandbox.arun(
                cmd=f"bash -c {shlex.quote(extract_cmd)}",
                mode=RunMode.NOHUP,
                wait_timeout=extract_timeout,
            )
            if res.exit_code != 0:
                return Observation(exit_code=1, failure_reason=f"tar extract failed: {res.output}")

            # cleanup remote tarball
            try:
                await self.sandbox.execute(Command(command=["rm", "-f", remote_tar_path]))
            except Exception:
                pass

            return Observation(exit_code=0, output=f"uploaded {src} -> {target_dir} via tar")

        except Exception as e:
            return Observation(exit_code=1, failure_reason=f"upload_dir unexpected error: {e}")

        finally:
            # cleanup local tarball
            try:
                if local_tar_path and local_tar_path.exists():
                    local_tar_path.unlink()
            except Exception:
                pass
