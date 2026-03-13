import shlex
import tarfile
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path

import oss2

from rock import env_vars
from rock.actions import CreateBashSessionRequest, Observation
from rock.actions.sandbox.base import AbstractSandbox
from rock.actions.sandbox.request import ChmodRequest, ChownRequest, Command
from rock.actions.sandbox.response import ChmodResponse, ChownResponse, CommandResponse, DownloadFileResponse
from rock.logger import init_logger
from rock.sdk.sandbox.constants import ENSURE_OSSUTIL_SCRIPT

logger = init_logger(__name__)


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

    @abstractmethod
    async def download_file(
        self,
        remote_path: str,
        local_path: str | Path,
    ) -> DownloadFileResponse:
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

    async def download_file(
        self,
        remote_path: str,
        local_path: str | Path,
    ) -> DownloadFileResponse:
        """Download file from sandbox container to local machine using OSS as intermediary.

        Flow:
        1. Check OSS is enabled via ROCK_OSS_ENABLE
        2. Verify source file exists in sandbox container (must be a regular file, not directory)
        3. Ensure ossutil is installed (auto-install if missing, checks wget/curl/unzip)
        4. Verify ossutil is working by running 'ossutil version'
        5. Get STS credentials from sandbox via get_token API
        6. Upload file from sandbox to OSS using ossutil (with STS token)
        7. Download file from OSS to local using oss2 (with same STS token)
        8. Verify downloaded file exists locally

        Note:
            - Only supports regular files, NOT directories. To download a directory, first create a
              tar archive in the sandbox (e.g., `tar czf /tmp/mydir.tar.gz /path/to/mydir`), then
              download the tar file.
            - Temporary OSS object is NOT cleaned up automatically (aligns with _upload_via_oss behavior).
              Consider using OSS lifecycle policies for automatic cleanup.

        Args:
            remote_path: File path inside the sandbox container (absolute path recommended, must be a regular file)
            local_path: Target file path on the local machine (supports ~/ expansion)

        Returns:
            DownloadFileResponse with success status and message

        Raises:
            AssertionError: If sandbox is not an instance of Sandbox class
        """
        from rock.sdk.sandbox.client import RunMode, Sandbox

        # Assert sandbox is Sandbox instance
        assert isinstance(self.sandbox, Sandbox), "sandbox must be an instance of Sandbox"

        timestamp = str(time.time_ns())

        try:
            # Check OSS enable
            if not env_vars.ROCK_OSS_ENABLE:
                return DownloadFileResponse(
                    success=False, message="OSS download is not enabled. Please set ROCK_OSS_ENABLE=true"
                )

            # Check remote file exists (must be a regular file, not directory)
            check_response: CommandResponse = await self.sandbox.execute(Command(command=["test", "-f", remote_path]))
            if check_response.exit_code != 0:
                return DownloadFileResponse(
                    success=False,
                    message=f"Source file not found or is not a regular file in sandbox: {remote_path}. "
                    "Note: Only regular files are supported. For directories, create a tar archive first.",
                )

            # Ensure ossutil is installed (checks wget/curl, unzip, installs if missing)
            ensure_response = await self.sandbox.process.execute_script(
                script_content=ENSURE_OSSUTIL_SCRIPT,
                script_name=f"ensure_ossutil_{timestamp}.sh",
                cleanup=True,
            )
            if ensure_response.exit_code != 0:
                return DownloadFileResponse(
                    success=False, message=f"Failed to ensure ossutil: {ensure_response.output}"
                )

            # Verify ossutil is actually working
            verify_response: CommandResponse = await self.sandbox.execute(Command(command=["ossutil", "version"]))
            if verify_response.exit_code != 0:
                return DownloadFileResponse(
                    success=False,
                    message=f"ossutil verification failed (exit_code={verify_response.exit_code}): {verify_response.stderr}",
                )
            logger.debug(f"ossutil verified: {verify_response.stdout.strip()}")

            # Get STS credentials from sandbox (for both ossutil upload and oss2 download)
            try:
                credentials = await self.sandbox._get_oss_sts_credentials()
            except Exception as e:
                return DownloadFileResponse(success=False, message=f"Failed to get OSS STS token: {e}")

            access_key_id = credentials["AccessKeyId"]
            access_key_secret = credentials["AccessKeySecret"]
            security_token = credentials["SecurityToken"]

            endpoint = env_vars.ROCK_OSS_BUCKET_ENDPOINT
            bucket_name = env_vars.ROCK_OSS_BUCKET_NAME
            region = env_vars.ROCK_OSS_BUCKET_REGION

            # Upload from sandbox to OSS via ossutil
            file_name = remote_path.split("/")[-1]
            tmp_obj_name = f"{timestamp}-{file_name}"
            oss_url = f"oss://{bucket_name}/{tmp_obj_name}"

            # Build ossutil command with bash -c wrapper (required for nohup mode)
            ossutil_inner_cmd = (
                f"ossutil cp {shlex.quote(remote_path)} {shlex.quote(oss_url)}"
                f" --access-key-id {shlex.quote(access_key_id)}"
                f" --access-key-secret {shlex.quote(access_key_secret)}"
                f" --sts-token {shlex.quote(security_token)}"
                f" --endpoint {shlex.quote(endpoint)}"
                f" --region {shlex.quote(region)}"
            )
            ossutil_cmd = f"bash -c {shlex.quote(ossutil_inner_cmd)}"
            logger.debug(f"Uploading {remote_path} to OSS via ossutil")
            upload_response = await self.sandbox.arun(cmd=ossutil_cmd, mode=RunMode.NOHUP)
            if upload_response.exit_code != 0:
                return DownloadFileResponse(
                    success=False,
                    message=f"Failed to upload file to OSS (exit_code={upload_response.exit_code}): {upload_response.output}",
                )
            logger.debug(f"ossutil upload completed: {upload_response.output}")

            # Download from OSS to local via oss2
            oss_auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
            oss_bucket = oss2.Bucket(oss_auth, endpoint, bucket_name, region=region)

            local = Path(local_path).expanduser().resolve()
            local.parent.mkdir(parents=True, exist_ok=True)
            try:
                oss_bucket.get_object_to_file(tmp_obj_name, str(local))
            except Exception as e:
                return DownloadFileResponse(success=False, message=f"Failed to download from OSS: {str(e)}")

            # Verify local file exists
            if not local.exists():
                return DownloadFileResponse(success=False, message=f"Downloaded file not found at: {local}")

            # Note: OSS temporary object is NOT cleaned up here to align with _upload_via_oss behavior
            return DownloadFileResponse(success=True, message=f"Successfully downloaded {remote_path} to {local}")

        except Exception as e:
            logger.exception(f"Unexpected error during download_by_oss: {e}")
            return DownloadFileResponse(success=False, message=f"Unexpected error: {str(e)}")
