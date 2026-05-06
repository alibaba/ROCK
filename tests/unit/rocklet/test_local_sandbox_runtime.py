from pathlib import Path

import gem
import pytest
from gem.envs.game_env.sokoban import SokobanEnv

from rock.actions import EnvMakeResponse, EnvStepResponse, UploadRequest
from rock.admin.proto.request import SandboxBashAction as BashAction
from rock.admin.proto.request import SandboxCloseBashSessionRequest as CloseBashSessionRequest
from rock.admin.proto.request import SandboxCreateBashSessionRequest as CreateBashSessionRequest
from rock.admin.proto.request import SandboxReadFileRequest as ReadFileRequest
from rock.rocklet.rocklet import Rocklet


@pytest.fixture
def local_runtime():
    return Rocklet.create()


@pytest.mark.asyncio
async def test_upload_file(local_runtime: Rocklet, tmp_path: Path):
    file_path = tmp_path / "source.txt"
    file_path.write_text("test")
    tmp_target = tmp_path / "target.txt"
    await local_runtime.upload(UploadRequest(source_path=str(file_path), target_path=str(tmp_target)))
    assert (await local_runtime.read_file(ReadFileRequest(path=str(tmp_target)))).content == "test"


@pytest.mark.asyncio
async def test_upload_directory(local_runtime: Rocklet, tmp_path: Path):
    dir_path = tmp_path / "source_dir"
    dir_path.mkdir()
    (dir_path / "file1.txt").write_text("test1")
    (dir_path / "file2.txt").write_text("test2")
    tmp_target = tmp_path / "target_dir"
    await local_runtime.upload(UploadRequest(source_path=str(dir_path), target_path=str(tmp_target)))
    assert (await local_runtime.read_file(ReadFileRequest(path=str(tmp_target / "file1.txt")))).content == "test1"
    assert (await local_runtime.read_file(ReadFileRequest(path=str(tmp_target / "file2.txt")))).content == "test2"


@pytest.mark.asyncio
async def test_gem(local_runtime: Rocklet):
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
async def test_prompt_command(local_runtime: Rocklet):
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


# --- cgroup CPU sampling -----------------------------------------------------


def test_sample_cpu_cores_first_call_returns_zero(monkeypatch, local_runtime: LocalSandboxRuntime):
    """First call has no baseline; should return 0.0 even if cgroup is readable."""
    monkeypatch.setattr("rock.rocklet.local_sandbox._read_cgroup_cpu_ns", lambda: 1_000_000_000)
    assert local_runtime._sample_cpu_cores() == 0.0


def test_sample_cpu_cores_returns_zero_when_cgroup_unreadable(monkeypatch, local_runtime: LocalSandboxRuntime):
    monkeypatch.setattr("rock.rocklet.local_sandbox._read_cgroup_cpu_ns", lambda: None)
    # Even with a prior baseline, an unreadable second sample yields 0.
    local_runtime._cpu_prev_ns = 1_000_000_000
    local_runtime._cpu_prev_ts = 100.0
    assert local_runtime._sample_cpu_cores() == 0.0


def test_sample_cpu_cores_computes_delta_average(monkeypatch, local_runtime: LocalSandboxRuntime):
    """1.5 core-seconds consumed over 1 wall-clock second -> 1.5 cores average."""
    # Seed prior reading: 0 ns at t=100s
    local_runtime._cpu_prev_ns = 0
    local_runtime._cpu_prev_ts = 100.0
    # New reading: 1.5e9 ns (1.5 core-seconds) at t=101s
    monkeypatch.setattr("rock.rocklet.local_sandbox._read_cgroup_cpu_ns", lambda: 1_500_000_000)
    monkeypatch.setattr("rock.rocklet.local_sandbox.time.monotonic", lambda: 101.0)

    assert local_runtime._sample_cpu_cores() == pytest.approx(1.5)


def test_sample_cpu_cores_clamps_negative_delta_to_zero(monkeypatch, local_runtime: LocalSandboxRuntime):
    """A non-monotonic kernel reading must not produce a negative core count."""
    local_runtime._cpu_prev_ns = 2_000_000_000
    local_runtime._cpu_prev_ts = 100.0
    monkeypatch.setattr("rock.rocklet.local_sandbox._read_cgroup_cpu_ns", lambda: 1_000_000_000)
    monkeypatch.setattr("rock.rocklet.local_sandbox.time.monotonic", lambda: 101.0)
    assert local_runtime._sample_cpu_cores() == 0.0
