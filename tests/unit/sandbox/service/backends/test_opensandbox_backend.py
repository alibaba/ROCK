from io import BytesIO, IOBase
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import UploadFile

from rock.actions.sandbox.response import State
from rock.admin.proto.request import (
    SandboxBashAction,
    SandboxCommand,
    SandboxCreateBashSessionRequest,
    SandboxReadFileRequest,
    SandboxWriteFileRequest,
)
from rock.rocklet.exceptions import CommandTimeoutError, NonZeroExitCodeError
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
async def test_timeout_execution_raises_existing_timeout_error(client):
    client.execute.return_value = _execution(
        stderr="command exceeded deadline",
        exit_code=None,
        error=SimpleNamespace(name="TimeoutError", value="command timed out"),
    )
    backend = OpenSandboxBackend(client)

    with pytest.raises(CommandTimeoutError, match=r"Timeout \(9.0s\)"):
        await backend.execute(
            "sbx-1",
            _info(),
            SandboxCommand(command="sleep 10", timeout=9, sandbox_id="sbx-1"),
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


class NonIOStream:
    def __init__(self, data: bytes):
        self._stream = NoReadAll(data)

    def read(self, size=-1):
        return self._stream.read(size)

    def seek(self, offset, whence=0):
        return self._stream.seek(offset, whence)

    def seekable(self):
        return True


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


@pytest.mark.asyncio
async def test_upload_adapts_non_iobase_stream_without_buffering(client):
    client.get_file_info.return_value = {}
    backend = OpenSandboxBackend(client)
    stream = NonIOStream(b"payload")
    upload = UploadFile(file=stream, filename="payload.bin")

    await backend.upload("sbx-1", _info(), upload, "/tmp/payload.bin")

    passed_stream = client.write_file.await_args.args[2]
    assert isinstance(passed_stream, IOBase)
    assert passed_stream.read(3) == b"pay"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remote_state", "expected"),
    [
        ("Pending", State.PENDING),
        ("Running", State.RUNNING),
        ("Paused", State.STOPPED),
        ("Terminated", State.DELETED),
        ("Failed", State.STOPPED),
    ],
)
async def test_get_state_maps_opensandbox_lifecycle(client, remote_state, expected):
    client.get_state.return_value = remote_state
    backend = OpenSandboxBackend(client)

    assert await backend.get_state(_info()) == expected


@pytest.mark.asyncio
async def test_create_session_uses_container_user_and_does_not_copy_admin_env(client):
    client.create_session.return_value = "session-1"
    backend = OpenSandboxBackend(client)

    session_id, response = await backend.create_session(
        "sbx-1",
        _info(),
        SandboxCreateBashSessionRequest(session="default", env_enable=True, sandbox_id="sbx-1"),
    )

    assert session_id == "session-1"
    assert response.output == ""
    client.create_session.assert_awaited_once_with("osb-1")
    client.execute.assert_not_awaited()
    client.run_in_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_session_initializes_explicit_env_and_startup_sources(client):
    client.create_session.return_value = "session-1"
    client.run_in_session.return_value = _execution(stdout="ready\n")
    backend = OpenSandboxBackend(client)

    _, response = await backend.create_session(
        "sbx-1",
        _info(),
        SandboxCreateBashSessionRequest(
            session="default",
            env={"GREETING": "hello world"},
            startup_source=["/work/profile with spaces.sh"],
            startup_timeout=3,
            sandbox_id="sbx-1",
        ),
    )

    assert response.output == "ready\n"
    assert client.run_in_session.await_args.args == (
        "osb-1",
        "session-1",
        "export GREETING='hello world' && source '/work/profile with spaces.sh'",
    )
    assert client.run_in_session.await_args.kwargs == {"timeout": 3.0}


@pytest.mark.asyncio
async def test_create_session_allows_explicit_effective_user(client):
    client.execute.return_value = _execution(stdout="sandbox\n")
    client.create_session.return_value = "session-1"
    backend = OpenSandboxBackend(client)

    await backend.create_session(
        "sbx-1",
        _info(),
        SandboxCreateBashSessionRequest(session="default", remote_user="sandbox", sandbox_id="sbx-1"),
    )

    client.execute.assert_awaited_once_with("osb-1", "id -un")
    client.create_session.assert_awaited_once_with("osb-1")


@pytest.mark.asyncio
async def test_create_session_rejects_different_remote_user_before_creation(client):
    client.execute.return_value = _execution(stdout="sandbox\n")
    backend = OpenSandboxBackend(client)

    with pytest.raises(BadRequestRockError, match="remote_user=root.*effective user is sandbox"):
        await backend.create_session(
            "sbx-1",
            _info(),
            SandboxCreateBashSessionRequest(session="default", remote_user="root", sandbox_id="sbx-1"),
        )

    client.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_session_deletes_remote_session_when_initialization_fails(client):
    client.create_session.return_value = "session-1"
    client.run_in_session.return_value = _execution(stderr="missing", exit_code=1)
    backend = OpenSandboxBackend(client)

    with pytest.raises(NonZeroExitCodeError, match="initialize"):
        await backend.create_session(
            "sbx-1",
            _info(),
            SandboxCreateBashSessionRequest(
                session="default",
                startup_source=["/missing"],
                sandbox_id="sbx-1",
            ),
        )

    client.delete_session.assert_awaited_once_with("osb-1", "session-1")


@pytest.mark.asyncio
async def test_create_session_maps_initialization_timeout_and_cleans_up(client):
    client.create_session.return_value = "session-1"
    client.run_in_session.return_value = _execution(
        exit_code=None,
        error=SimpleNamespace(name="TimeoutError", value="deadline exceeded"),
    )
    backend = OpenSandboxBackend(client)

    with pytest.raises(CommandTimeoutError, match=r"Timeout \(3.0s\)"):
        await backend.create_session(
            "sbx-1",
            _info(),
            SandboxCreateBashSessionRequest(
                session="default",
                startup_source=["/slow"],
                startup_timeout=3,
                sandbox_id="sbx-1",
            ),
        )

    client.delete_session.assert_awaited_once_with("osb-1", "session-1")


@pytest.mark.asyncio
async def test_run_session_maps_output_and_check_policy(client):
    client.run_in_session.return_value = _execution(stdout="out", stderr="err", exit_code=2)
    backend = OpenSandboxBackend(client)
    action = SandboxBashAction(command="false", session="default", check="silent", sandbox_id="sbx-1")

    response = await backend.run_session("sbx-1", _info(), "session-1", action)

    assert response.output == "outerr"
    assert response.exit_code == 2
    assert response.failure_reason == ""
    client.run_in_session.assert_awaited_once_with("osb-1", "session-1", "false", timeout=None)


@pytest.mark.asyncio
async def test_run_session_check_raise_uses_existing_error(client):
    client.run_in_session.return_value = _execution(stderr="failed", exit_code=2)
    backend = OpenSandboxBackend(client)

    with pytest.raises(NonZeroExitCodeError, match="prefix"):
        await backend.run_session(
            "sbx-1",
            _info(),
            "session-1",
            SandboxBashAction(
                command="false",
                session="default",
                check="raise",
                error_msg="prefix",
                sandbox_id="sbx-1",
            ),
        )


@pytest.mark.asyncio
async def test_run_session_check_ignore_omits_exit_code(client):
    client.run_in_session.return_value = _execution(stderr="failed", exit_code=2)
    backend = OpenSandboxBackend(client)

    response = await backend.run_session(
        "sbx-1",
        _info(),
        "session-1",
        SandboxBashAction(command="false", session="default", check="ignore", sandbox_id="sbx-1"),
    )

    assert response.exit_code is None
    assert response.output == "failed"


@pytest.mark.asyncio
async def test_run_session_rejects_interactive_mode(client):
    backend = OpenSandboxBackend(client)

    with pytest.raises(BadRequestRockError, match="interactive"):
        await backend.run_session(
            "sbx-1",
            _info(),
            "session-1",
            SandboxBashAction(
                command="python",
                session="default",
                is_interactive_command=True,
                sandbox_id="sbx-1",
            ),
        )


@pytest.mark.asyncio
async def test_close_session_deletes_remote_session(client):
    backend = OpenSandboxBackend(client)

    response = await backend.close_session("sbx-1", _info(), "session-1")

    assert response.session_type == "bash"
    client.delete_session.assert_awaited_once_with("osb-1", "session-1")


@pytest.mark.asyncio
async def test_get_endpoint_uses_opensandbox_id(client):
    client.get_endpoint.return_value = SimpleNamespace(endpoint="sandbox.example", headers={"X-Token": "required"})
    backend = OpenSandboxBackend(client)

    endpoint = await backend.get_endpoint("sbx-1", _info("osb-9"), 8080)

    assert endpoint.endpoint == "sandbox.example"
    client.get_endpoint.assert_awaited_once_with("osb-9", 8080)
