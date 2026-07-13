from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from rock.admin.proto.request import SandboxCommand
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
