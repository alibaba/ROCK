"""StorageClient — download archived sandbox log tarballs from OSS.

Separate from :class:`rock.sdk.sandbox.oss_client.OssClient` because they serve
different OSS prefixes and lifecycles:
  - OssClient        — sandbox <-> host file transfer (rock-transfer/), alive sandbox
  - StorageClient    — stopped sandbox log archives (rock-archives/), historical

Recovery flow:
  1. Call admin ``/get_token?account=primary`` to obtain a short-lived STS
     for the primary OSS account.
  2. Use oss2 + STS auth to download
     ``oss://<bucket>/<archive_prefix>sandbox-logs/<sandbox_id>.tar.gz``.

The CLI (``rock storage get``) is a thin wrapper around this client.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import oss2

from rock.logger import init_logger
from rock.utils.archive_command import ArchiveCommand
from rock.utils.http import HttpUtils

logger = init_logger(__name__)


class ArchiveNotFoundError(FileNotFoundError):
    """Raised when the requested sandbox's archive does not exist in OSS."""


class StorageClient:
    """SDK client for downloading archived sandbox log tarballs from OSS.

    Args:
        base_url: admin base URL. May be the bare host (``https://admin/``) or
            include the ``/apis/envs/sandbox/v1`` prefix; both are accepted.
        auth_token: optional ``xrl-authorization`` token for admin requests.
        extra_headers: optional extra HTTP headers attached to every admin call.
    """

    _API_PREFIX = "/apis/envs/sandbox/v1"

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        self._base_url = base_url
        self._auth_token = auth_token
        self._extra_headers = dict(extra_headers or {})

    async def download_archived_log(
        self,
        sandbox_id: str,
        output_path: str,
        archive_prefix: str = "",
        bucket: str | None = None,
        endpoint: str | None = None,
    ) -> str:
        """Download ``sandbox_id``'s archived log tarball to ``output_path``.

        Args:
            sandbox_id: target sandbox id (matches the log directory name).
            output_path: local file path to write the downloaded tarball.
            archive_prefix: OSS key prefix used at archive time. Must match
                admin's ``sandbox_config.log.archive_prefix``.
            bucket: optional OSS bucket override (defaults to value returned
                by admin ``/get_token``).
            endpoint: optional OSS endpoint override (same default rule).

        Returns:
            The local path the tarball was written to (== ``output_path``).

        Raises:
            ArchiveNotFoundError: archive object does not exist in OSS.
            RuntimeError: STS fetch failed or admin response missing fields.
        """
        sts = await self._fetch_primary_sts()
        bucket_name, endpoint_url, region = self._extract_oss_target(sts, bucket, endpoint)
        oss_key = ArchiveCommand.build_key(sandbox_id, archive_prefix)

        oss_bucket = oss2.Bucket(
            auth=oss2.StsAuth(sts["AccessKeyId"], sts["AccessKeySecret"], sts["SecurityToken"]),
            endpoint=endpoint_url,
            bucket_name=bucket_name,
            region=region,
        )

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        try:
            await asyncio.to_thread(oss_bucket.get_object_to_file, oss_key, output_path)
        except oss2.exceptions.NoSuchKey as e:
            raise ArchiveNotFoundError(f"oss://{bucket_name}/{oss_key}") from e

        return output_path

    async def _fetch_primary_sts(self) -> dict[str, Any]:
        url = self._build_get_token_url()
        headers = self._build_headers()
        try:
            response = await HttpUtils.get(url, headers)
        except Exception as e:
            # Most common 404 cause: caller passed admin-write URL but /get_token only
            # lives on the proxy/read role. Augment the error so the user knows what to flip.
            if "404" in str(e):
                raise RuntimeError(
                    f"admin /get_token returned 404 at {url}. "
                    "/get_token is only mounted on the proxy/read admin role, not the write admin. "
                    "If you used the write URL, switch base_url to the proxy/read URL."
                ) from e
            raise
        if response.get("status") != "Success":
            raise RuntimeError(f"admin /get_token returned: {response.get('message') or response}")
        result = response.get("result")
        if not result:
            raise RuntimeError("admin /get_token returned an empty result; check OssConfig.primary on admin")
        return result

    def _build_get_token_url(self) -> str:
        """Normalize ``base_url`` and append ``/get_token?account=primary``.

        Accepts either the bare admin host or a base that already contains
        the API prefix; in either case the result hits the right route.
        """
        clean = self._base_url.rstrip("/")
        if not clean.endswith(self._API_PREFIX):
            clean = f"{clean}{self._API_PREFIX}"
        return f"{clean}/get_token?account=primary"

    def _build_headers(self) -> dict[str, str]:
        headers = dict(self._extra_headers)
        if self._auth_token:
            headers["xrl-authorization"] = self._auth_token
        return headers

    @staticmethod
    def _extract_oss_target(
        sts: dict[str, Any],
        bucket_override: str | None,
        endpoint_override: str | None,
    ) -> tuple[str, str, str | None]:
        # The /get_token response from a recent admin includes Endpoint/Bucket/Region
        # for the primary account. Caller overrides win (useful when testing against
        # a non-default bucket without redeploying admin).
        bucket = bucket_override or sts.get("Bucket")
        endpoint = endpoint_override or sts.get("Endpoint")
        region = sts.get("Region")
        if not bucket or not endpoint:
            raise RuntimeError(
                "OSS bucket/endpoint missing — pass bucket/endpoint explicitly or configure "
                "OssConfig.primary.bucket/endpoint on admin"
            )
        return bucket, endpoint, region
