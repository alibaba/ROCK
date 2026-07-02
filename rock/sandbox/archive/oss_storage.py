import asyncio
import os
import shutil
import tempfile

import oss2

from rock.logger import init_logger
from rock.sandbox.archive.abstract import AbstractDirStorage

logger = init_logger(__name__)


class OssDirStorage(AbstractDirStorage):
    """OSS Access Point directory storage using oss2 SDK + AuthV4."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
        region: str = "cn-hangzhou",
    ):
        self._endpoint = endpoint
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._region = region

    @property
    def client_config(self) -> dict:
        return {
            "type": "oss",
            "endpoint": self._endpoint,
            "bucket": self._bucket,
            "access_key_id": self._access_key_id,
            "access_key_secret": self._access_key_secret,
            "region": self._region,
        }

    def _make_bucket(self) -> oss2.Bucket:
        auth = oss2.AuthV4(self._access_key_id, self._access_key_secret)
        return oss2.Bucket(auth, self._endpoint, self._bucket, region=self._region)

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

            bucket = self._make_bucket()
            await asyncio.to_thread(oss2.resumable_upload, bucket, key, tmp_tar)
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

            bucket = self._make_bucket()
            try:
                await asyncio.to_thread(bucket.get_object_to_file, key, tmp_tar)
            except oss2.exceptions.NoSuchKey:
                raise FileNotFoundError(f"Key not found: {key}")

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
        bucket = self._make_bucket()
        if not await asyncio.to_thread(bucket.object_exists, key):
            logger.info(f"delete: key {key} not found in {self._bucket}, skipping")
            return False
        await asyncio.to_thread(bucket.delete_object, key)
        logger.info(f"delete: removed key {key} from {self._bucket}")
        return True

    async def exists(self, key: str) -> bool:
        bucket = self._make_bucket()
        found = await asyncio.to_thread(bucket.object_exists, key)
        logger.info(f"exists: key {key} {'found' if found else 'not found'} in {self._bucket}")
        return found
