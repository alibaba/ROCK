"""Tests for OssClient — encapsulates all OSS operations for Sandbox."""

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock import env_vars
from rock.sdk.sandbox._oss_client import OssClient, OssClientConfig


def _make_sandbox(base_url="http://admin:8080", headers=None):
    sb = MagicMock()
    sb._url = base_url
    sb._build_headers = MagicMock(return_value=headers or {})
    return sb


def test_oss_client_module_imports():
    assert OssClient is not None
    assert OssClientConfig is not None


class TestComputeObjectName:
    def test_format_is_hash_dash_filename(self):
        name = OssClient._compute_object_name("sb-1", "/local/file.json", "/sandbox/file.json")
        assert re.match(r"^[0-9a-f]{64}-file\.json$", name)

    def test_deterministic_same_inputs_same_output(self):
        a = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/x")
        b = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/x")
        assert a == b

    def test_different_sandbox_id_yields_different_hash(self):
        a = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/x")
        b = OssClient._compute_object_name("sb-2", "/local/x", "/sandbox/x")
        assert a != b

    def test_different_local_path_yields_different_hash(self):
        a = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/x")
        b = OssClient._compute_object_name("sb-1", "/local/y", "/sandbox/x")
        assert a != b

    def test_different_sandbox_path_yields_different_hash(self):
        a = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/x")
        b = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/y")
        assert a != b

    def test_filename_is_basename_of_local_path(self):
        name = OssClient._compute_object_name("sb-1", "/dir1/dir2/foo.txt", "/other/bar.txt")
        assert name.endswith("-foo.txt")

    def test_filename_falls_back_to_sandbox_path_basename_when_local_empty(self):
        name = OssClient._compute_object_name("sb-1", "", "/sandbox/baz.txt")
        assert name.endswith("-baz.txt")


class TestResolveConfig:
    def test_layer1_env_takes_precedence_over_server(self):
        with (
            patch.object(env_vars, "ROCK_OSS_BUCKET_ENDPOINT", "env.endpoint"),
            patch.object(env_vars, "ROCK_OSS_BUCKET_NAME", "env-bucket"),
            patch.object(env_vars, "ROCK_OSS_BUCKET_REGION", "env-region"),
        ):
            cfg = OssClient._resolve_config(
                {
                    "Endpoint": "srv.endpoint",
                    "Bucket": "srv-bucket",
                    "Region": "srv-region",
                }
            )
        assert cfg.endpoint == "env.endpoint"
        assert cfg.bucket == "env-bucket"
        assert cfg.region == "env-region"
        assert cfg.enabled_via_env is True

    def test_layer2_used_when_env_not_all_set(self):
        with (
            patch.object(env_vars, "ROCK_OSS_BUCKET_ENDPOINT", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_NAME", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_REGION", ""),
        ):
            cfg = OssClient._resolve_config(
                {
                    "Endpoint": "srv.endpoint",
                    "Bucket": "srv-bucket",
                    "Region": "srv-region",
                }
            )
        assert cfg.endpoint == "srv.endpoint"
        assert cfg.enabled_via_env is False

    def test_partial_env_does_not_promote_to_layer1(self):
        # 只设了 endpoint，bucket / region 缺失 → 不算 Layer 1，回到 Layer 2
        with (
            patch.object(env_vars, "ROCK_OSS_BUCKET_ENDPOINT", "env.endpoint"),
            patch.object(env_vars, "ROCK_OSS_BUCKET_NAME", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_REGION", ""),
        ):
            cfg = OssClient._resolve_config(
                {
                    "Endpoint": "srv.endpoint",
                    "Bucket": "srv-bucket",
                    "Region": "srv-region",
                }
            )
        assert cfg.endpoint == "srv.endpoint"
        assert cfg.enabled_via_env is False

    def test_layer3_returns_none_when_neither_layer_complete(self):
        with (
            patch.object(env_vars, "ROCK_OSS_BUCKET_ENDPOINT", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_NAME", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_REGION", ""),
        ):
            cfg = OssClient._resolve_config({"Endpoint": None, "Bucket": None, "Region": None})
        assert cfg is None

    def test_server_partial_treated_as_unavailable(self):
        # 服务端只返回 endpoint/bucket，没 region → Layer 2 不齐 → 不可用
        with (
            patch.object(env_vars, "ROCK_OSS_BUCKET_ENDPOINT", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_NAME", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_REGION", ""),
        ):
            cfg = OssClient._resolve_config({"Endpoint": "x", "Bucket": "y", "Region": None})
        assert cfg is None

    def test_empty_dict_returns_none(self):
        with (
            patch.object(env_vars, "ROCK_OSS_BUCKET_ENDPOINT", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_NAME", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_REGION", ""),
        ):
            cfg = OssClient._resolve_config({})
        assert cfg is None


class TestGetStsCredentials:
    async def test_success_returns_credentials_dict(self):
        sandbox = _make_sandbox()
        client = OssClient(sandbox)

        mock_response = {
            "status": "Success",
            "result": {
                "AccessKeyId": "ak",
                "AccessKeySecret": "sk",
                "SecurityToken": "tok",
                "Expiration": "2026-12-31T00:00:00Z",
                "Endpoint": "endpoint",
                "Bucket": "bucket",
                "Region": "region",
            },
        }
        with patch("rock.sdk.sandbox._oss_client.HttpUtils") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_response)
            result = await client._get_sts_credentials()

        assert result["AccessKeyId"] == "ak"
        assert result["Endpoint"] == "endpoint"
        assert client._token_expire_time == "2026-12-31T00:00:00Z"

    async def test_failure_raises(self):
        sandbox = _make_sandbox()
        client = OssClient(sandbox)
        with patch("rock.sdk.sandbox._oss_client.HttpUtils") as mock_http:
            mock_http.get = AsyncMock(return_value={"status": "Fail", "message": "boom"})
            with pytest.raises(Exception, match="boom"):
                await client._get_sts_credentials()


class TestIsTokenExpired:
    def test_no_token_means_expired(self):
        client = OssClient(_make_sandbox())
        client._token_expire_time = None
        assert client._is_token_expired() is True

    def test_future_expiration_not_expired(self):
        client = OssClient(_make_sandbox())
        client._token_expire_time = "2099-01-01T00:00:00Z"
        assert client._is_token_expired() is False

    def test_past_expiration_is_expired(self):
        client = OssClient(_make_sandbox())
        client._token_expire_time = "2000-01-01T00:00:00Z"
        assert client._is_token_expired() is True

    def test_within_5min_buffer_is_expired(self):
        client = OssClient(_make_sandbox())
        # 未来 1 分钟（< 5 分钟 buffer）
        near_future = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client._token_expire_time = near_future
        assert client._is_token_expired() is True

    def test_attribute_error_is_treated_as_expired(self):
        client = OssClient(_make_sandbox())
        client._token_expire_time = 12345  # int, no .replace method → AttributeError
        assert client._is_token_expired() is True
