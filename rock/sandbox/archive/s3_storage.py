import asyncio
import os
import shutil
import tempfile

import boto3
from botocore.exceptions import ClientError

from rock.logger import init_logger
from rock.sandbox.archive.abstract import AbstractDirStorage

logger = init_logger(__name__)


class S3DirStorage(AbstractDirStorage):
    """S3-compatible directory storage (works with MinIO and OSS S3 endpoint)."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
        region: str = "us-east-1",
    ):
        self._endpoint = endpoint
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._region = region

    @property
    def client_config(self) -> dict:
        return {
            "type": "s3",
            "endpoint": self._endpoint,
            "bucket": self._bucket,
            "access_key_id": self._access_key_id,
            "access_key_secret": self._access_key_secret,
            "region": self._region,
        }

    def _make_client(self):
        return boto3.client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._access_key_secret,
            region_name=self._region,
        )

    async def upload_dir(self, local_dir: str, key: str) -> None:
        if not os.path.isdir(local_dir):
            raise FileNotFoundError(f"Directory not found: {local_dir}")

        parent = os.path.dirname(os.path.abspath(local_dir))
        basename = os.path.basename(os.path.abspath(local_dir))

        tmp_tar = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
                tmp_tar = f.name

            proc = await asyncio.create_subprocess_exec(
                "tar",
                "-czf",
                tmp_tar,
                "-C",
                parent,
                basename,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise
            if proc.returncode != 0:
                raise RuntimeError(f"tar failed: {stderr.decode()}")

            client = self._make_client()
            await asyncio.to_thread(client.upload_file, tmp_tar, self._bucket, key)
        finally:
            if tmp_tar and os.path.exists(tmp_tar):
                os.unlink(tmp_tar)

    async def download_to_dir(self, key: str, local_dir: str) -> None:
        if os.path.exists(local_dir):
            raise FileExistsError(f"Target directory already exists: {local_dir}")

        parent = os.path.dirname(os.path.abspath(local_dir))
        basename = os.path.basename(os.path.abspath(local_dir))
        os.makedirs(parent, exist_ok=True)

        tmp_tar = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
                tmp_tar = f.name

            client = self._make_client()
            try:
                await asyncio.to_thread(client.download_file, self._bucket, key, tmp_tar)
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    raise FileNotFoundError(f"Key not found: {key}")
                raise

            proc = await asyncio.create_subprocess_exec(
                "tar",
                "-xzf",
                tmp_tar,
                "-C",
                parent,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise
            if proc.returncode != 0:
                raise RuntimeError(f"tar extract failed: {stderr.decode()}")

            extracted = os.path.join(parent, basename)
            if not os.path.isdir(extracted):
                entries = os.listdir(parent)
                raise RuntimeError(f"Expected directory '{basename}' after extraction, got: {entries}")
        except Exception:
            if os.path.exists(local_dir):
                shutil.rmtree(local_dir, ignore_errors=True)
            raise
        finally:
            if tmp_tar and os.path.exists(tmp_tar):
                os.unlink(tmp_tar)

    async def delete(self, key: str) -> bool:
        if not await self.exists(key):
            logger.info(f"delete: key {key} not found in {self._bucket}, skipping")
            return False
        client = self._make_client()
        await asyncio.to_thread(client.delete_object, Bucket=self._bucket, Key=key)
        logger.info(f"delete: removed key {key} from {self._bucket}")
        return True

    async def exists(self, key: str) -> bool:
        client = self._make_client()
        try:
            await asyncio.to_thread(client.head_object, Bucket=self._bucket, Key=key)
            logger.info(f"exists: key {key} found in {self._bucket}")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                logger.info(f"exists: key {key} not found in {self._bucket}")
                return False
            raise
