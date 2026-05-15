"""Tests for OssClient — encapsulates all OSS operations for Sandbox."""

import re
from unittest.mock import patch

from rock import env_vars
from rock.sdk.sandbox._oss_client import OssClient, OssClientConfig


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
