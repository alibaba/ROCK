"""Tests for `rock storage get` — download archived sandbox log tarball."""

import argparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rock.cli.command.storage import StorageCommand


def _args(**overrides):
    base = dict(
        storage_action="get",
        sandbox_id="sb-abc",
        output=None,
        archive_prefix="rock-archives/",
        bucket=None,
        endpoint=None,
        base_url="http://admin.local:8080",
        auth_token="tok-1",
        extra_headers={"cluster": "default"},
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _sts_payload(**extra):
    payload = {
        "AccessKeyId": "AK",
        "AccessKeySecret": "SK",
        "SecurityToken": "ST",
        "Endpoint": "oss-cn-hangzhou.aliyuncs.com",
        "Bucket": "chatos-rock",
        "Region": "cn-hangzhou",
    }
    payload.update(extra)
    return payload


class TestStorageGet:
    @pytest.mark.asyncio
    async def test_success_downloads_to_default_path_and_uses_correct_key(self, tmp_path, capsys):
        cmd = StorageCommand()
        args = _args(output=str(tmp_path / "out.tar.gz"))

        bucket_mock = MagicMock()
        bucket_mock.get_object_to_file = MagicMock()
        with (
            patch(
                "rock.sdk.sandbox.storage_client.HttpUtils.get",
                AsyncMock(return_value={"status": "Success", "result": _sts_payload()}),
            ),
            patch("rock.sdk.sandbox.storage_client.oss2") as oss2_mod,
        ):
            oss2_mod.Bucket.return_value = bucket_mock
            oss2_mod.StsAuth.return_value = "stsauth"
            # NoSuchKey lookup path is exercised separately; here the call succeeds.
            oss2_mod.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

            await cmd.arun(args)

        # Asserts on what we sent to oss2:
        oss2_mod.StsAuth.assert_called_once_with("AK", "SK", "ST")
        oss2_mod.Bucket.assert_called_once()
        kwargs = oss2_mod.Bucket.call_args.kwargs
        assert kwargs["bucket_name"] == "chatos-rock"
        assert kwargs["endpoint"] == "oss-cn-hangzhou.aliyuncs.com"
        assert kwargs["region"] == "cn-hangzhou"

        # Key is built via shared helper, must match archive-side layout exactly.
        bucket_mock.get_object_to_file.assert_called_once()
        oss_key, local_path = bucket_mock.get_object_to_file.call_args.args
        assert oss_key == "rock-archives/sandbox-logs/sb-abc.tar.gz"
        assert local_path == str(tmp_path / "out.tar.gz")

        out = capsys.readouterr().out
        assert "OK:" in out
        assert "tar -xzf" in out

    @pytest.mark.asyncio
    async def test_default_output_when_no_flag(self, capsys):
        cmd = StorageCommand()
        args = _args(output=None)

        bucket_mock = MagicMock()
        with (
            patch(
                "rock.sdk.sandbox.storage_client.HttpUtils.get",
                AsyncMock(return_value={"status": "Success", "result": _sts_payload()}),
            ),
            patch("rock.sdk.sandbox.storage_client.oss2") as oss2_mod,
            patch("rock.sdk.sandbox.storage_client.os.makedirs"),
        ):
            oss2_mod.Bucket.return_value = bucket_mock
            oss2_mod.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

            await cmd.arun(args)

        _, local_path = bucket_mock.get_object_to_file.call_args.args
        assert local_path == "./sb-abc.tar.gz"

    @pytest.mark.asyncio
    async def test_directory_output_appends_filename(self, capsys):
        cmd = StorageCommand()
        args = _args(output="/tmp/recover/")  # trailing slash → directory semantics

        bucket_mock = MagicMock()
        with (
            patch(
                "rock.sdk.sandbox.storage_client.HttpUtils.get",
                AsyncMock(return_value={"status": "Success", "result": _sts_payload()}),
            ),
            patch("rock.sdk.sandbox.storage_client.oss2") as oss2_mod,
            patch("rock.sdk.sandbox.storage_client.os.makedirs"),
        ):
            oss2_mod.Bucket.return_value = bucket_mock
            oss2_mod.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

            await cmd.arun(args)

        _, local_path = bucket_mock.get_object_to_file.call_args.args
        assert local_path == "/tmp/recover/sb-abc.tar.gz"

    @pytest.mark.asyncio
    async def test_no_such_key_prints_not_found(self, capsys):
        cmd = StorageCommand()
        args = _args()

        # Build the NoSuchKey class first so storage.py can raise it via the patched module.
        no_such_key = type("NoSuchKey", (Exception,), {})
        bucket_mock = MagicMock()
        bucket_mock.get_object_to_file = MagicMock(side_effect=no_such_key("missing"))

        with (
            patch(
                "rock.sdk.sandbox.storage_client.HttpUtils.get",
                AsyncMock(return_value={"status": "Success", "result": _sts_payload()}),
            ),
            patch("rock.sdk.sandbox.storage_client.oss2") as oss2_mod,
            patch("rock.sdk.sandbox.storage_client.os.makedirs"),
        ):
            oss2_mod.Bucket.return_value = bucket_mock
            oss2_mod.exceptions.NoSuchKey = no_such_key

            await cmd.arun(args)

        out = capsys.readouterr().out
        assert "NOT FOUND" in out
        assert "rock-archives/sandbox-logs/sb-abc.tar.gz" in out

    @pytest.mark.asyncio
    async def test_generic_oss_exception_prints_failed(self, capsys):
        cmd = StorageCommand()
        args = _args()

        bucket_mock = MagicMock()
        # Use IOError (subclass of OSError, not RuntimeError) — real oss2 errors
        # surface as oss2.exceptions.OssError/ServerError, which inherit from
        # Exception (not RuntimeError). CLI swallows these and prints FAILED;
        # RuntimeError is reserved for pre-flight (STS/config) errors and bubbles.
        bucket_mock.get_object_to_file = MagicMock(side_effect=IOError("network exploded"))

        with (
            patch(
                "rock.sdk.sandbox.storage_client.HttpUtils.get",
                AsyncMock(return_value={"status": "Success", "result": _sts_payload()}),
            ),
            patch("rock.sdk.sandbox.storage_client.oss2") as oss2_mod,
            patch("rock.sdk.sandbox.storage_client.os.makedirs"),
        ):
            oss2_mod.Bucket.return_value = bucket_mock
            oss2_mod.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

            await cmd.arun(args)

        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "network exploded" in out

    @pytest.mark.asyncio
    async def test_admin_get_token_failure_raises(self):
        cmd = StorageCommand()
        args = _args()
        with patch(
            "rock.sdk.sandbox.storage_client.HttpUtils.get",
            AsyncMock(return_value={"status": "Failed", "message": "no primary configured"}),
        ):
            with pytest.raises(RuntimeError, match="no primary configured"):
                await cmd.arun(args)

    @pytest.mark.asyncio
    async def test_missing_oss_target_raises(self):
        """If admin returns STS without Endpoint/Bucket and no CLI override, fail loud."""
        cmd = StorageCommand()
        args = _args(bucket=None, endpoint=None)

        bare = _sts_payload()
        bare.pop("Endpoint")
        bare.pop("Bucket")
        with patch(
            "rock.sdk.sandbox.storage_client.HttpUtils.get",
            AsyncMock(return_value={"status": "Success", "result": bare}),
        ):
            with pytest.raises(RuntimeError, match="bucket/endpoint missing"):
                await cmd.arun(args)

    @pytest.mark.asyncio
    async def test_cli_overrides_take_precedence_over_get_token_response(self):
        cmd = StorageCommand()
        args = _args(bucket="my-bucket", endpoint="my-endpoint")

        bucket_mock = MagicMock()
        with (
            patch(
                "rock.sdk.sandbox.storage_client.HttpUtils.get",
                AsyncMock(return_value={"status": "Success", "result": _sts_payload()}),
            ),
            patch("rock.sdk.sandbox.storage_client.oss2") as oss2_mod,
            patch("rock.sdk.sandbox.storage_client.os.makedirs"),
        ):
            oss2_mod.Bucket.return_value = bucket_mock
            oss2_mod.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

            await cmd.arun(args)

        kwargs = oss2_mod.Bucket.call_args.kwargs
        assert kwargs["bucket_name"] == "my-bucket"
        assert kwargs["endpoint"] == "my-endpoint"

    @pytest.mark.asyncio
    async def test_unknown_action_raises(self):
        cmd = StorageCommand()
        with pytest.raises(ValueError, match="Unknown storage action"):
            await cmd.arun(argparse.Namespace(storage_action="delete"))

    @pytest.mark.asyncio
    async def test_404_error_message_explains_proxy_role(self):
        # A 404 on /get_token nearly always means the user pointed at the write-admin URL
        # by mistake; surface that explicitly instead of bubbling a raw HTTPStatusError.
        # Integration-level: CLI → SDK → patched HttpUtils → RuntimeError surfaces.
        cmd = StorageCommand()
        args = _args()
        with patch(
            "rock.sdk.sandbox.storage_client.HttpUtils.get",
            AsyncMock(side_effect=Exception("Client error '404 Not Found' for url ...")),
        ):
            with pytest.raises(RuntimeError, match="proxy/read admin role"):
                await cmd.arun(args)
