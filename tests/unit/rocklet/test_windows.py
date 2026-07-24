import io
from unittest.mock import MagicMock

from rock.admin.proto.request import SandboxBashAction, SandboxCreateBashSessionRequest
from rock.rocklet.windows import PowerShellSession


async def test_powershell_session_configures_non_echoing_input(monkeypatch):
    process = MagicMock()
    process.stdin = io.StringIO()
    process.stdout = io.StringIO()
    popen = MagicMock(return_value=process)
    monkeypatch.setattr("rock.rocklet.windows.subprocess.Popen", popen)
    monkeypatch.setattr("rock.rocklet.windows.time.sleep", lambda _: None)
    monkeypatch.setattr(PowerShellSession, "_find_powershell", staticmethod(lambda: "powershell"))
    monkeypatch.setattr(PowerShellSession, "_drain_queue", lambda self, timeout=0.1: "")

    session = PowerShellSession(SandboxCreateBashSessionRequest(session="test", sandbox_id="sandbox"))
    await session.start()

    command = popen.call_args.args[0]
    setup = command[command.index("-Command") + 1]
    assert "function global:PSConsoleHostReadLine { [Console]::In.ReadLine() }" in setup
    assert PowerShellSession._PROMPT_MARKER in setup


def test_powershell_session_removes_internal_prompt_from_output(monkeypatch):
    session = PowerShellSession(SandboxCreateBashSessionRequest(session="test", sandbox_id="sandbox"))
    process = MagicMock()
    process.stdin = io.StringIO()
    session._process = process
    monkeypatch.setattr(session, "_drain_queue", lambda timeout=0.1: "")

    prompt = "ROCKLET_PS_PROMPT_29234"
    session._output_queue.put(f"{prompt}{session._BEGIN_MARKER}\n")
    session._output_queue.put(f"{prompt}ROCK_REAL_OUTPUT\n")
    session._output_queue.put(f"{prompt}{session._EXIT_MARKER}0\n")
    session._output_queue.put(f"{prompt}{session._END_MARKER}\n")

    result = session._run_command(
        SandboxBashAction(command="Write-Output 'ROCK_REAL_OUTPUT'", session="test", sandbox_id="sandbox")
    )

    assert result.output == "ROCK_REAL_OUTPUT"
    assert result.exit_code == 0
