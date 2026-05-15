"""Tests for OssClient — encapsulates all OSS operations for Sandbox."""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock import env_vars
from rock.actions.sandbox.response import DownloadFileResponse, UploadResponse
from rock.sdk.sandbox.oss_client import OssClient, OssClientConfig


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

    def test_filename_is_basename_of_sandbox_path(self):
        name = OssClient._compute_object_name("sb-1", "/dir1/dir2/foo.txt", "/other/bar.txt")
        assert name.endswith("-bar.txt")

    def test_filename_falls_back_to_local_path_basename_when_sandbox_empty(self):
        name = OssClient._compute_object_name("sb-1", "/local/baz.txt", "")
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
        # Only endpoint set; bucket / region missing -> not Layer 1, fall back to Layer 2
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
        # Server returns endpoint/bucket but no region -> Layer 2 incomplete -> unavailable
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
        with patch("rock.sdk.sandbox.oss_client.HttpUtils") as mock_http:
            mock_http.get = AsyncMock(return_value=mock_response)
            result = await client._get_sts_credentials()

        assert result["AccessKeyId"] == "ak"
        assert result["Endpoint"] == "endpoint"
        assert client._token_expire_time == "2026-12-31T00:00:00Z"

    async def test_failure_raises(self):
        sandbox = _make_sandbox()
        client = OssClient(sandbox)
        with patch("rock.sdk.sandbox.oss_client.HttpUtils") as mock_http:
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
        # 1 minute in the future (within the 5-minute buffer)
        near_future = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client._token_expire_time = near_future
        assert client._is_token_expired() is True

    def test_attribute_error_is_treated_as_expired(self):
        client = OssClient(_make_sandbox())
        client._token_expire_time = 12345  # int, no .replace method → AttributeError
        assert client._is_token_expired() is True


class TestSetup:
    async def test_layer1_env_with_enable_true(self):
        sandbox = _make_sandbox()
        client = OssClient(sandbox)

        with (
            patch.object(env_vars, "ROCK_OSS_BUCKET_ENDPOINT", "env.endpoint"),
            patch.object(env_vars, "ROCK_OSS_BUCKET_NAME", "env-bucket"),
            patch.object(env_vars, "ROCK_OSS_BUCKET_REGION", "env-region"),
            patch.object(env_vars, "ROCK_OSS_ENABLE", True),
            patch("rock.sdk.sandbox.oss_client.HttpUtils") as mock_http,
            patch("rock.sdk.sandbox.oss_client.oss2") as mock_oss2,
        ):
            mock_http.get = AsyncMock(
                return_value={
                    "status": "Success",
                    "result": {
                        "AccessKeyId": "ak",
                        "AccessKeySecret": "sk",
                        "SecurityToken": "tok",
                        "Expiration": "2099-01-01T00:00:00Z",
                    },
                }
            )
            mock_oss2.Bucket = MagicMock(return_value="bucket-instance")
            ok = await client.ensure_setup()

        assert ok is True
        assert client.is_available is True
        mock_oss2.Bucket.assert_called_once()
        kwargs = mock_oss2.Bucket.call_args.kwargs
        assert kwargs["endpoint"] == "env.endpoint"
        assert kwargs["bucket_name"] == "env-bucket"

    async def test_layer1_env_with_enable_false_returns_unavailable(self):
        sandbox = _make_sandbox()
        client = OssClient(sandbox)

        with (
            patch.object(env_vars, "ROCK_OSS_BUCKET_ENDPOINT", "env.endpoint"),
            patch.object(env_vars, "ROCK_OSS_BUCKET_NAME", "env-bucket"),
            patch.object(env_vars, "ROCK_OSS_BUCKET_REGION", "env-region"),
            patch.object(env_vars, "ROCK_OSS_ENABLE", False),
            patch("rock.sdk.sandbox.oss_client.HttpUtils") as mock_http,
        ):
            mock_http.get = AsyncMock(
                return_value={
                    "status": "Success",
                    "result": {
                        "AccessKeyId": "ak",
                        "AccessKeySecret": "sk",
                        "SecurityToken": "tok",
                        "Expiration": "2099-01-01T00:00:00Z",
                    },
                }
            )
            ok = await client.ensure_setup()

        assert ok is False
        assert client.is_available is False

    async def test_layer2_server_response_used_when_env_unset(self):
        sandbox = _make_sandbox()
        client = OssClient(sandbox)

        with (
            patch.object(env_vars, "ROCK_OSS_BUCKET_ENDPOINT", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_NAME", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_REGION", ""),
            patch("rock.sdk.sandbox.oss_client.HttpUtils") as mock_http,
            patch("rock.sdk.sandbox.oss_client.oss2") as mock_oss2,
        ):
            mock_http.get = AsyncMock(
                return_value={
                    "status": "Success",
                    "result": {
                        "AccessKeyId": "ak",
                        "AccessKeySecret": "sk",
                        "SecurityToken": "tok",
                        "Expiration": "2099-01-01T00:00:00Z",
                        "Endpoint": "srv.endpoint",
                        "Bucket": "srv-bucket",
                        "Region": "srv-region",
                    },
                }
            )
            mock_oss2.Bucket = MagicMock(return_value="bucket-instance")
            ok = await client.ensure_setup()

        assert ok is True
        assert client.is_available is True
        kwargs = mock_oss2.Bucket.call_args.kwargs
        assert kwargs["endpoint"] == "srv.endpoint"

    async def test_layer3_unavailable_when_neither_set(self):
        sandbox = _make_sandbox()
        client = OssClient(sandbox)

        with (
            patch.object(env_vars, "ROCK_OSS_BUCKET_ENDPOINT", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_NAME", ""),
            patch.object(env_vars, "ROCK_OSS_BUCKET_REGION", ""),
            patch("rock.sdk.sandbox.oss_client.HttpUtils") as mock_http,
        ):
            mock_http.get = AsyncMock(
                return_value={
                    "status": "Success",
                    "result": {
                        "AccessKeyId": "ak",
                        "AccessKeySecret": "sk",
                        "SecurityToken": "tok",
                        "Expiration": "2099-01-01T00:00:00Z",
                    },
                }
            )
            ok = await client.ensure_setup()

        assert ok is False
        assert client.is_available is False

    async def test_ensure_setup_idempotent_when_token_valid(self):
        sandbox = _make_sandbox()
        client = OssClient(sandbox)
        client._bucket = MagicMock()  # already set up
        client._token_expire_time = "2099-01-01T00:00:00Z"

        with patch("rock.sdk.sandbox.oss_client.HttpUtils") as mock_http:
            mock_http.get = AsyncMock()
            ok = await client.ensure_setup()

        assert ok is True
        mock_http.get.assert_not_called()  # /get_token not re-invoked


class TestUploadViaOss:
    async def test_success(self):
        sandbox = _make_sandbox()
        sandbox.sandbox_id = "sb-123"
        sandbox.arun = AsyncMock(return_value=MagicMock(exit_code=0))
        sandbox.execute = AsyncMock(return_value=MagicMock(exit_code=0))

        client = OssClient(sandbox)
        client._bucket = MagicMock()
        client._bucket.sign_url = MagicMock(return_value="https://oss/signed?...")

        with patch("rock.sdk.sandbox.oss_client.oss2.resumable_upload") as mock_upload:
            response = await client.upload_via_oss("/local/foo.json", "/sandbox/dst/foo.json")

        assert isinstance(response, UploadResponse)
        assert response.success is True
        # Verify OSS object naming follows the new convention: sha256-filename
        expected_obj = OssClient._compute_object_name("sb-123", "/local/foo.json", "/sandbox/dst/foo.json")
        mock_upload.assert_called_once()
        # resumable_upload(bucket, obj_name, file_path)
        assert mock_upload.call_args.args[1] == expected_obj
        assert mock_upload.call_args.args[2] == "/local/foo.json"

    async def test_sandbox_verification_fail_returns_failure(self):
        sandbox = _make_sandbox()
        sandbox.sandbox_id = "sb-123"
        # mkdir/wget go through arun and succeed; the final test -f check goes
        # through execute() and fails (exit_code=1 = file missing).
        sandbox.arun = AsyncMock(return_value=MagicMock(exit_code=0))
        sandbox.execute = AsyncMock(return_value=MagicMock(exit_code=1))

        client = OssClient(sandbox)
        client._bucket = MagicMock()
        client._bucket.sign_url = MagicMock(return_value="url")

        with patch("rock.sdk.sandbox.oss_client.oss2.resumable_upload"):
            response = await client.upload_via_oss("/local/foo.json", "/sandbox/dst/foo.json")

        assert response.success is False
        assert "sandbox download phase failed" in response.message

    async def test_oss_upload_exception_returns_failure(self):
        sandbox = _make_sandbox()
        sandbox.sandbox_id = "sb-123"
        client = OssClient(sandbox)
        client._bucket = MagicMock()

        with patch("rock.sdk.sandbox.oss_client.oss2.resumable_upload", side_effect=Exception("oss boom")):
            response = await client.upload_via_oss("/local/foo.json", "/sandbox/dst/foo.json")

        assert response.success is False


class TestDownloadViaOss:
    async def test_oss_unavailable_returns_failure(self, tmp_path):
        sandbox = _make_sandbox()
        client = OssClient(sandbox)
        # _bucket is still None
        response = await client.download_via_oss("/sandbox/foo.txt", tmp_path / "foo.txt")
        assert isinstance(response, DownloadFileResponse)
        assert response.success is False
        assert "OSS is not available" in response.message

    async def test_remote_file_not_found_returns_failure(self, tmp_path):
        sandbox = _make_sandbox()
        sandbox.sandbox_id = "sb-1"
        sandbox.execute = AsyncMock(return_value=MagicMock(exit_code=1))  # test -f fails

        client = OssClient(sandbox)
        client._bucket = MagicMock()
        client._client_config = OssClientConfig("ep", "bk", "rg", enabled_via_env=False)

        response = await client.download_via_oss("/sandbox/foo.txt", tmp_path / "foo.txt")
        assert response.success is False
        assert "not found" in response.message.lower()


class TestClose:
    async def test_close_with_no_pending_tasks_is_noop(self):
        client = OssClient(_make_sandbox())
        await client.close()  # should not raise


class TestScheduleAsyncPersistence:
    async def test_schedules_task_when_available(self):
        sandbox = _make_sandbox()
        sandbox.sandbox_id = "sb-1"
        client = OssClient(sandbox)
        client._bucket = MagicMock()

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = None
            key = await client.schedule_async_persistence("/local/foo.json", "/sandbox/foo.json")
            # wait for task to complete (must be within the patch scope)
            await asyncio.gather(*client._pending_persistence_tasks, return_exceptions=True)
            mock_to_thread.assert_awaited_once()

        assert key.endswith("-foo.json")

    async def test_no_op_when_unavailable(self):
        client = OssClient(_make_sandbox())
        # _bucket is still None
        key = await client.schedule_async_persistence("/local/foo.json", "/sandbox/foo.json")
        assert key is None
        assert len(client._pending_persistence_tasks) == 0

    async def test_failure_does_not_raise(self):
        """OSS upload failure is swallowed; main flow is unaffected."""
        sandbox = _make_sandbox()
        sandbox.sandbox_id = "sb-1"
        client = OssClient(sandbox)
        client._bucket = MagicMock()

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.side_effect = Exception("oss boom")
            key = await client.schedule_async_persistence("/local/foo.json", "/sandbox/foo.json")
            # wait for task to complete (must not raise)
            await asyncio.gather(*client._pending_persistence_tasks, return_exceptions=True)

        assert key is not None


class TestCloseAwaitsPendingTasks:
    async def test_close_awaits_completion(self):
        sandbox = _make_sandbox()
        sandbox.sandbox_id = "sb-1"
        client = OssClient(sandbox)
        client._bucket = MagicMock()

        completion_marker = asyncio.Event()

        async def slow_upload(*args, **kwargs):
            await asyncio.sleep(0.1)
            completion_marker.set()

        with patch("asyncio.to_thread", new=slow_upload):
            await client.schedule_async_persistence("/local/foo.json", "/sandbox/foo.json")
            assert not completion_marker.is_set()
            await client.close()
            assert completion_marker.is_set()

    async def test_close_timeout_does_not_hang(self):
        """close does not hang or raise on timeout (pending tasks beyond timeout are abandoned)."""
        sandbox = _make_sandbox()
        sandbox.sandbox_id = "sb-1"
        client = OssClient(sandbox)
        client._bucket = MagicMock()

        async def hang(*args, **kwargs):
            await asyncio.sleep(100)

        with patch("asyncio.to_thread", new=hang):
            await client.schedule_async_persistence("/local/foo.json", "/sandbox/foo.json")
            await client.close(timeout=0.05)


# prefix propagation
def test_compute_object_name_with_prefix():
    name = OssClient._compute_object_name(
        sandbox_id="sb-1",
        local_path="/tmp/x",
        sandbox_path="/data/x",
        prefix="rock-transfer/",
    )
    assert name.startswith("rock-transfer/")
    assert name.endswith("-x")


def test_compute_object_name_strips_slashes_in_prefix():
    name = OssClient._compute_object_name(
        sandbox_id="sb-1",
        local_path="/tmp/x",
        sandbox_path="/data/x",
        prefix="/rock-transfer//",
    )
    assert name.startswith("rock-transfer/")
    assert "//" not in name


def test_compute_object_name_no_prefix_keeps_legacy_layout():
    name = OssClient._compute_object_name(
        sandbox_id="sb-1",
        local_path="/tmp/x",
        sandbox_path="/data/x",
    )
    assert "/" not in name  # flat layout
