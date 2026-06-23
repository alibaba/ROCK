"""Tests for OssClient — encapsulates all OSS operations for Sandbox."""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    def test_complete_server_response_returns_config(self):
        cfg = OssClient._resolve_config(
            {"Endpoint": "srv.endpoint", "Bucket": "srv-bucket", "Region": "srv-region"}
        )
        assert cfg is not None
        assert cfg.endpoint == "srv.endpoint"
        assert cfg.bucket == "srv-bucket"
        assert cfg.region == "srv-region"
        assert cfg.prefix == ""

    def test_server_response_with_prefix(self):
        cfg = OssClient._resolve_config(
            {"Endpoint": "srv", "Bucket": "b", "Region": "r", "Prefix": "rock-transfer/"}
        )
        assert cfg is not None
        assert cfg.prefix == "rock-transfer/"

    def test_partial_server_response_returns_none(self):
        # Missing Region → incomplete
        cfg = OssClient._resolve_config({"Endpoint": "srv", "Bucket": "b", "Region": None})
        assert cfg is None

    def test_empty_response_returns_none(self):
        cfg = OssClient._resolve_config({})
        assert cfg is None

    def test_all_none_returns_none(self):
        cfg = OssClient._resolve_config({"Endpoint": None, "Bucket": None, "Region": None})
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
    async def test_server_config_sets_up_bucket(self):
        sandbox = _make_sandbox()
        client = OssClient(sandbox)

        with (
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
        mock_oss2.Bucket.assert_called_once()
        kwargs = mock_oss2.Bucket.call_args.kwargs
        assert kwargs["endpoint"] == "srv.endpoint"
        assert kwargs["bucket_name"] == "srv-bucket"
        assert kwargs["region"] == "srv-region"

    async def test_unavailable_when_server_does_not_return_config(self):
        sandbox = _make_sandbox()
        client = OssClient(sandbox)

        with patch("rock.sdk.sandbox.oss_client.HttpUtils") as mock_http:
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

    async def test_wget_command_has_no_continue_flag(self):
        """Regression: `wget -c` would skip download when target exists with
        matching size — but OSS object key is path-based (not content-based),
        so a repeat upload re-uses the same key and `-c` would leave the
        sandbox file at its old content. We must use `-O` alone to force
        truncate-and-rewrite."""
        sandbox = _make_sandbox()
        sandbox.sandbox_id = "sb-123"
        sandbox.arun = AsyncMock(return_value=MagicMock(exit_code=0))
        sandbox.execute = AsyncMock(return_value=MagicMock(exit_code=0))

        client = OssClient(sandbox)
        client._bucket = MagicMock()
        client._bucket.sign_url = MagicMock(return_value="https://oss/signed?token=xxx")

        with patch("rock.sdk.sandbox.oss_client.oss2.resumable_upload"):
            await client.upload_via_oss("/local/foo.json", "/sandbox/dst/foo.json")

        # arun() is invoked twice: first for `mkdir -p`, second for `wget`.
        wget_call = next(
            c for c in sandbox.arun.await_args_list
            if "wget" in (c.kwargs.get("cmd") or (c.args[0] if c.args else ""))
        )
        wget_cmd = wget_call.kwargs.get("cmd") or wget_call.args[0]
        # The bug was `wget -c -O ...` — `-c` makes wget skip a same-sized local file.
        assert " -c " not in f" {wget_cmd} ", f"wget should not use -c: {wget_cmd!r}"
        assert " -O " in wget_cmd, f"wget must use -O to force overwrite: {wget_cmd!r}"

    async def test_wget_command_quotes_target_path(self):
        """target_path is interpolated into a shell command; if it contains
        spaces or shell metachars (`;`, `$`, `&`, etc.) the unquoted form
        would either fail or, worse, execute injected commands. shlex.quote
        is the standard fix."""
        sandbox = _make_sandbox()
        sandbox.sandbox_id = "sb-123"
        sandbox.arun = AsyncMock(return_value=MagicMock(exit_code=0))
        sandbox.execute = AsyncMock(return_value=MagicMock(exit_code=0))

        client = OssClient(sandbox)
        client._bucket = MagicMock()
        client._bucket.sign_url = MagicMock(return_value="https://oss/signed")

        unsafe_target = "/sandbox/dir with spaces/foo.json"
        with patch("rock.sdk.sandbox.oss_client.oss2.resumable_upload"):
            await client.upload_via_oss("/local/foo.json", unsafe_target)

        wget_call = next(
            c for c in sandbox.arun.await_args_list
            if "wget" in (c.kwargs.get("cmd") or (c.args[0] if c.args else ""))
        )
        wget_cmd = wget_call.kwargs.get("cmd") or wget_call.args[0]
        # shlex.quote wraps a path-with-spaces in single quotes.
        assert "'/sandbox/dir with spaces/foo.json'" in wget_cmd, \
            f"target_path with spaces must be shell-quoted: {wget_cmd!r}"

    async def test_repeat_upload_to_same_target_does_not_skip_via_wget(self):
        """End-to-end intent: repeat upload(local_v2, /tmp/x) after upload(local_v1, /tmp/x)
        must issue a wget command that *will* overwrite — i.e. wget must be invoked
        with -O alone (not -c -O). We can't observe sandbox file content in unit
        tests, but verifying the command shape forces the correct behavior."""
        sandbox = _make_sandbox()
        sandbox.sandbox_id = "sb-fixed"
        sandbox.arun = AsyncMock(return_value=MagicMock(exit_code=0))
        sandbox.execute = AsyncMock(return_value=MagicMock(exit_code=0))

        client = OssClient(sandbox)
        client._bucket = MagicMock()
        client._bucket.sign_url = MagicMock(return_value="https://oss/v1")

        with patch("rock.sdk.sandbox.oss_client.oss2.resumable_upload"):
            # Round 1: upload local_a → /sandbox/x
            await client.upload_via_oss("/local/a.bin", "/sandbox/x.bin")
            # Round 2: upload local_b (different content, hypothetically) → SAME target
            client._bucket.sign_url = MagicMock(return_value="https://oss/v2")
            await client.upload_via_oss("/local/b.bin", "/sandbox/x.bin")

        # Both rounds must produce a wget cmd without `-c`.
        wget_calls = [
            (c.kwargs.get("cmd") or (c.args[0] if c.args else ""))
            for c in sandbox.arun.await_args_list
            if "wget" in (c.kwargs.get("cmd") or (c.args[0] if c.args else ""))
        ]
        assert len(wget_calls) == 2
        for wget_cmd in wget_calls:
            assert " -c " not in f" {wget_cmd} ", f"`-c` regressed: {wget_cmd!r}"


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
        client._client_config = OssClientConfig("ep", "bk", "rg")

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


def test_compute_object_name_whitespace_only_prefix_treated_as_empty():
    """A prefix that is just whitespace must NOT produce '   /<digest>-x'."""
    name = OssClient._compute_object_name(
        sandbox_id="sb-1",
        local_path="/tmp/x",
        sandbox_path="/data/x",
        prefix="   ",
    )
    assert "/" not in name
    assert " " not in name
    assert name.endswith("-x")


def test_compute_object_name_collapses_internal_double_slashes():
    """Server-pushed prefix with stray '//' should be normalized in the OSS key."""
    name = OssClient._compute_object_name(
        sandbox_id="sb-1",
        local_path="/tmp/x",
        sandbox_path="/data/x",
        prefix="rock//transfer///",
    )
    assert name.startswith("rock/transfer/")
    assert "//" not in name


def test_compute_object_name_none_prefix_is_treated_as_empty():
    name = OssClient._compute_object_name(
        sandbox_id="sb-1",
        local_path="/tmp/x",
        sandbox_path="/data/x",
        prefix=None,
    )
    assert "/" not in name


def test_compute_object_name_empty_string_prefix_is_treated_as_empty():
    name = OssClient._compute_object_name(
        sandbox_id="sb-1",
        local_path="/tmp/x",
        sandbox_path="/data/x",
        prefix="",
    )
    assert "/" not in name
