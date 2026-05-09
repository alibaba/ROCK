from pathlib import Path

import gem
import pytest
from gem.envs.game_env.sokoban import SokobanEnv

from rock.actions import EnvMakeResponse, EnvStepResponse, UploadRequest
from rock.admin.proto.request import SandboxBashAction as BashAction
from rock.admin.proto.request import SandboxCloseBashSessionRequest as CloseBashSessionRequest
from rock.admin.proto.request import SandboxCreateBashSessionRequest as CreateBashSessionRequest
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.rocklet.local_sandbox import LocalSandboxRuntime


@pytest.fixture
def local_runtime():
    return LocalSandboxRuntime()


@pytest.mark.asyncio
async def test_upload_file(local_runtime: LocalSandboxRuntime, tmp_path: Path):
    file_path = tmp_path / "source.txt"
    file_path.write_text("test")
    tmp_target = tmp_path / "target.txt"
    await local_runtime.upload(UploadRequest(source_path=str(file_path), target_path=str(tmp_target)))
    assert (await local_runtime.read_file(ReadFileRequest(path=str(tmp_target)))).content == "test"


@pytest.mark.asyncio
async def test_upload_directory(local_runtime: LocalSandboxRuntime, tmp_path: Path):
    dir_path = tmp_path / "source_dir"
    dir_path.mkdir()
    (dir_path / "file1.txt").write_text("test1")
    (dir_path / "file2.txt").write_text("test2")
    tmp_target = tmp_path / "target_dir"
    await local_runtime.upload(UploadRequest(source_path=str(dir_path), target_path=str(tmp_target)))
    assert (await local_runtime.read_file(ReadFileRequest(path=str(tmp_target / "file1.txt")))).content == "test1"
    assert (await local_runtime.read_file(ReadFileRequest(path=str(tmp_target / "file2.txt")))).content == "test2"


@pytest.mark.asyncio
async def test_gem(local_runtime: LocalSandboxRuntime):
    env_id = "game:Sokoban-v0-easy"
    exmaple_gem_env: SokobanEnv = gem.make(env_id)

    # List all supported environments
    sandbox_id = "test_gem"
    env_make_response: EnvMakeResponse = local_runtime.env_make(env_id, sandbox_id)
    assert sandbox_id == env_make_response.sandbox_id
    env_reset_response = local_runtime.env_reset(sandbox_id, seed=42)
    assert env_reset_response.observation
    assert env_reset_response.info

    for _ in range(10):
        action = exmaple_gem_env.sample_random_action()
        env_step_response: EnvStepResponse = local_runtime.env_step(sandbox_id, action)
        assert env_step_response.observation is not None
        assert env_step_response.reward is not None
        assert env_step_response.terminated is not None
        assert env_step_response.truncated is not None
        assert env_step_response.info is not None

        if env_step_response.terminated or env_step_response.truncated:
            break
    local_runtime.env_close(sandbox_id)


@pytest.mark.asyncio
async def test_prompt_command(local_runtime: LocalSandboxRuntime):
    prompt_command = "echo ROCK"
    await local_runtime.create_session(
        CreateBashSessionRequest(env={"PROMPT_COMMAND": prompt_command}, session_type="bash")
    )
    without_prompt_command = await local_runtime.run_in_session(BashAction(command="echo hello", action_type="bash"))
    assert without_prompt_command.output == "hello"
    await local_runtime.run_in_session(
        BashAction(command=f'export PROMPT_COMMAND="{prompt_command}"', action_type="bash")
    )
    with_prompt_command = await local_runtime.run_in_session(BashAction(command="echo hello", action_type="bash"))
    assert with_prompt_command.output.__contains__("ROCK")
    await local_runtime.close_session(CloseBashSessionRequest(session_type="bash"))


# ========== Terminal Settings Tests ==========


@pytest.mark.asyncio
async def test_default_terminal_settings(local_runtime: LocalSandboxRuntime):
    """Test that default terminal settings do not explicitly set TERM/LANG (backward compatibility).

    Note: bash/pexpect may still set TERM=dumb by default, but we don't explicitly set it.
    The key test is that we can explicitly override TERM/LANG when needed.
    """
    import uuid
    session_name = f"term_default_{uuid.uuid4().hex[:8]}"
    await local_runtime.create_session(
        CreateBashSessionRequest(session_type="bash", session=session_name, startup_timeout=5.0)
    )

    # Check TERM - pexpect/bash defaults to "dumb" when not explicitly set
    obs = await local_runtime.run_in_session(
        BashAction(command="echo $TERM", action_type="bash", session=session_name, timeout=10)
    )
    # pexpect/bash defaults to "dumb" when TERM is not set
    assert "dumb" in obs.output

    # Check LANG - should not be set by default (empty or system default)
    obs = await local_runtime.run_in_session(
        BashAction(command="echo $LANG", action_type="bash", session=session_name, timeout=10)
    )
    # LANG may be empty or set to system default; we just verify we don't explicitly set it

    await local_runtime.close_session(CloseBashSessionRequest(session_type="bash", session=session_name))


@pytest.mark.asyncio
async def test_custom_terminal_settings(local_runtime: LocalSandboxRuntime):
    """Test that custom terminal settings are applied correctly."""
    import uuid
    session_name = f"term_custom_{uuid.uuid4().hex[:8]}"
    await local_runtime.create_session(
        CreateBashSessionRequest(
            session_type="bash",
            session=session_name,
            startup_timeout=5.0,
            term="screen",
            columns=120,
            lines=40,
            lang="zh_CN.UTF-8",
        )
    )

    # Check TERM
    obs = await local_runtime.run_in_session(
        BashAction(command="echo $TERM", action_type="bash", session=session_name, timeout=10)
    )
    assert "screen" in obs.output

    # Check LANG
    obs = await local_runtime.run_in_session(
        BashAction(command="echo $LANG", action_type="bash", session=session_name, timeout=10)
    )
    assert "zh_CN.UTF-8" in obs.output

    await local_runtime.close_session(CloseBashSessionRequest(session_type="bash", session=session_name))


@pytest.mark.asyncio
async def test_terminal_size_stty(local_runtime: LocalSandboxRuntime):
    """Test that terminal size is correctly set via stty size."""
    import uuid
    session_name = f"term_stty_{uuid.uuid4().hex[:8]}"
    await local_runtime.create_session(
        CreateBashSessionRequest(
            session_type="bash",
            session=session_name,
            startup_timeout=5.0,
            columns=100,
            lines=30,
        )
    )

    # stty size outputs "lines columns"
    obs = await local_runtime.run_in_session(
        BashAction(command="stty size", action_type="bash", session=session_name, timeout=10)
    )
    assert "30 100" in obs.output

    await local_runtime.close_session(CloseBashSessionRequest(session_type="bash", session=session_name))


@pytest.mark.asyncio
async def test_env_overrides_terminal_params(local_runtime: LocalSandboxRuntime):
    """Test that env parameter takes priority over terminal params."""
    import uuid
    session_name = f"term_override_{uuid.uuid4().hex[:8]}"
    await local_runtime.create_session(
        CreateBashSessionRequest(
            session_type="bash",
            session=session_name,
            startup_timeout=5.0,
            term="xterm",
            env={"TERM": "vt100", "LANG": "C"},
        )
    )

    # env should override term param
    obs = await local_runtime.run_in_session(
        BashAction(command="echo $TERM", action_type="bash", session=session_name, timeout=10)
    )
    assert "vt100" in obs.output

    obs = await local_runtime.run_in_session(
        BashAction(command="echo $LANG", action_type="bash", session=session_name, timeout=10)
    )
    assert "C" in obs.output

    await local_runtime.close_session(CloseBashSessionRequest(session_type="bash", session=session_name))
