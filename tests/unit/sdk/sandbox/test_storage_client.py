"""SDK-level tests for StorageClient (URL/headers/STS extraction pure logic).

Integration-level oss2/HttpUtils mocking is exercised end-to-end via
tests/unit/cli/command/test_storage.py (CLI -> SDK -> mocked oss2 path).
This file focuses on the pure helpers + auth header logic that don't need
the full download path.
"""

import pytest

from rock.sdk.sandbox.storage_client import StorageClient


class TestBuildGetTokenUrl:
    def test_appends_api_prefix_when_base_url_is_bare_host(self):
        client = StorageClient(base_url="https://admin.local")
        assert client._build_get_token_url() == "https://admin.local/apis/envs/sandbox/v1/get_token?account=primary"

    def test_does_not_double_append_when_base_url_already_has_prefix(self):
        client = StorageClient(base_url="https://admin.local/apis/envs/sandbox/v1")
        assert client._build_get_token_url() == "https://admin.local/apis/envs/sandbox/v1/get_token?account=primary"

    def test_strips_trailing_slash(self):
        client = StorageClient(base_url="https://admin.local/")
        assert client._build_get_token_url() == "https://admin.local/apis/envs/sandbox/v1/get_token?account=primary"


class TestBuildHeaders:
    def test_auth_token_lifts_to_xrl_authorization_header(self):
        client = StorageClient(base_url="https://x", auth_token="tok-2", extra_headers={"cluster": "c1"})
        headers = client._build_headers()
        assert headers["xrl-authorization"] == "tok-2"
        assert headers["cluster"] == "c1"

    def test_no_auth_token_keeps_extra_headers_only(self):
        client = StorageClient(base_url="https://x", auth_token=None, extra_headers={"cluster": "c1"})
        headers = client._build_headers()
        assert "xrl-authorization" not in headers
        assert headers["cluster"] == "c1"

    def test_extra_headers_isolated_from_caller_mutation(self):
        # Defensive copy: caller mutating their dict after construction must not
        # leak into subsequent client requests.
        original = {"cluster": "c1"}
        client = StorageClient(base_url="https://x", extra_headers=original)
        original["leaked"] = "yes"
        assert "leaked" not in client._build_headers()


class TestExtractOssTarget:
    _STS_FULL = {
        "AccessKeyId": "AK",
        "AccessKeySecret": "SK",
        "SecurityToken": "ST",
        "Endpoint": "oss-cn-hangzhou.aliyuncs.com",
        "Bucket": "chatos-rock",
        "Region": "cn-hangzhou",
    }

    def test_caller_bucket_override_wins(self):
        bucket, endpoint, region = StorageClient._extract_oss_target(self._STS_FULL, "override-bucket", None)
        assert bucket == "override-bucket"
        assert endpoint == self._STS_FULL["Endpoint"]
        assert region == "cn-hangzhou"

    def test_caller_endpoint_override_wins(self):
        bucket, endpoint, region = StorageClient._extract_oss_target(self._STS_FULL, None, "override-endpoint")
        assert bucket == self._STS_FULL["Bucket"]
        assert endpoint == "override-endpoint"

    def test_missing_bucket_raises(self):
        sts = dict(self._STS_FULL)
        del sts["Bucket"]
        with pytest.raises(RuntimeError, match="bucket/endpoint missing"):
            StorageClient._extract_oss_target(sts, None, None)

    def test_missing_endpoint_raises(self):
        sts = dict(self._STS_FULL)
        del sts["Endpoint"]
        with pytest.raises(RuntimeError, match="bucket/endpoint missing"):
            StorageClient._extract_oss_target(sts, None, None)
