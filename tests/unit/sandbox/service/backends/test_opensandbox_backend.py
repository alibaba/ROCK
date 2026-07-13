from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import UploadFile

from rock.admin.proto.request import SandboxCommand, SandboxReadFileRequest, SandboxWriteFileRequest
from rock.rocklet.exceptions import NonZeroExitCodeError
from rock.sandbox.service.backends.opensandbox import OpenSandboxBackend
from rock.sdk.common.exceptions import BadRequestRockError


def _info(opensandbox_id="osb-1"):
    return {"extended_params": {"backend": "opensandbox", "opensandbox_id": opensandbox_id}}


def _execution(*, stdout="", stderr="", exit_code=0, error=None):
    def messages(text):
        return [SimpleNamespace(text=text)] if text else []

    return SimpleNamespace(
        logs=SimpleNamespace(stdout=messages(stdout), stderr=messages(stderr)),
        exit_code=exit_code,
        error=error,
    )


@pytest.fixture
def client():
    result = AsyncMock()
    result.execute.return_value = _execution(stdout="ok\n")
    return result


@pytest.mark.asyncio
async def test_list_command_preserves_argument_boundaries(client):
    backend = OpenSandboxBackend(client)

    result = await backend.execute(
        "sbx-1",
        _info(),
        SandboxCommand(command=["echo", "hello world"], sandbox_id="sbx-1"),
    )

    assert result.stdout == "ok\n"
    assert client.execute.await_args.args[1] == "echo 'hello world'"


@pytest.mark.asyncio
async def test_string_shell_false_warns_without_logging_command(client, monkeypatch):
    backend = OpenSandboxBackend(client)
    warning = Mock()
    monkeypatch.setattr("rock.sandbox.service.backends.opensandbox.logger.warning", warning)

    await backend.execute(
        "sbx-1",
        _info(),
        SandboxCommand(command="secret-command", shell=False, sandbox_id="sbx-1"),
    )

    warning.assert_called_once()
    assert "shell=False" in warning.call_args.args[0]
    assert "secret-command" not in repr(warning.call_args)
    assert client.execute.await_args.args[1] == "secret-command"


@pytest.mark.asyncio
async def test_command_options_and_output_are_mapped(client):
    client.execute.return_value = _execution(stdout="out", stderr="err", exit_code=7)
    backend = OpenSandboxBackend(client)
    command = SandboxCommand(
        command="run",
        shell=True,
        timeout=9,
        cwd="/work",
        env={"A": "B"},
        sandbox_id="sbx-1",
    )

    result = await backend.execute("sbx-1", _info(), command)

    assert result.model_dump() == {"stdout": "out", "stderr": "err", "exit_code": 7}
    assert client.execute.await_args.kwargs == {"timeout": 9, "cwd": "/work", "env": {"A": "B"}}


@pytest.mark.asyncio
async def test_check_true_raises_existing_error(client):
    client.execute.return_value = _execution(stdout="out", stderr="err", exit_code=2)
    backend = OpenSandboxBackend(client)

    with pytest.raises(NonZeroExitCodeError, match="prefix"):
        await backend.execute(
            "sbx-1",
            _info(),
            SandboxCommand(command="false", shell=True, check=True, error_msg="prefix", sandbox_id="sbx-1"),
        )


@pytest.mark.asyncio
async def test_missing_opensandbox_id_fails_before_client_call(client):
    backend = OpenSandboxBackend(client)

    with pytest.raises(BadRequestRockError, match="opensandbox_id"):
        await backend.execute(
            "sbx-1",
            {"extended_params": {"backend": "opensandbox"}},
            SandboxCommand(command="pwd", sandbox_id="sbx-1"),
        )

    client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_file_decodes_bytes_with_requested_error_policy(client):
    client.read_bytes.return_value = b"hello\xffworld"
    backend = OpenSandboxBackend(client)

    result = await backend.read_file(
        "sbx-1",
        _info(),
        SandboxReadFileRequest(
            path="/tmp/a",
            encoding="utf-8",
            errors="replace",
            sandbox_id="sbx-1",
        ),
    )

    assert result.content == "hello�world"
    client.read_bytes.assert_awaited_once_with("osb-1", "/tmp/a")


@pytest.mark.asyncio
async def test_write_file_uses_644_for_new_file(client):
    client.get_file_info.return_value = {}
    backend = OpenSandboxBackend(client)

    result = await backend.write_file(
        "sbx-1",
        _info(),
        SandboxWriteFileRequest(path="/tmp/a", content="hello", sandbox_id="sbx-1"),
    )

    assert result.success is True
    client.write_file.assert_awaited_once_with("osb-1", "/tmp/a", "hello", mode=644)


@pytest.mark.asyncio
async def test_write_file_preserves_existing_mode(client):
    client.get_file_info.return_value = {"/tmp/a": SimpleNamespace(mode=755)}
    backend = OpenSandboxBackend(client)

    await backend.write_file(
        "sbx-1",
        _info(),
        SandboxWriteFileRequest(path="/tmp/a", content="hello", sandbox_id="sbx-1"),
    )

    client.write_file.assert_awaited_once_with("osb-1", "/tmp/a", "hello", mode=755)


@pytest.mark.asyncio
async def test_write_aborts_when_metadata_lookup_fails(client):
    client.get_file_info.side_effect = RuntimeError("metadata unavailable")
    backend = OpenSandboxBackend(client)

    with pytest.raises(RuntimeError, match="metadata unavailable"):
        await backend.write_file(
            "sbx-1",
            _info(),
            SandboxWriteFileRequest(path="/tmp/a", content="hello", sandbox_id="sbx-1"),
        )

    client.write_file.assert_not_awaited()


class NoReadAll(BytesIO):
    def read(self, size=-1):
        if size == -1:
            raise AssertionError("upload must not read the whole file")
        return super().read(size)


@pytest.mark.asyncio
async def test_upload_passes_stream_without_buffering(client):
    client.get_file_info.return_value = {}
    backend = OpenSandboxBackend(client)
    stream = NoReadAll(b"payload")
    upload = UploadFile(file=stream, filename="payload.bin")

    result = await backend.upload("sbx-1", _info(), upload, "/tmp/payload.bin")

    assert result.success is True
    assert result.file_name == "payload.bin"
    client.write_file.assert_awaited_once_with("osb-1", "/tmp/payload.bin", stream, mode=644)
