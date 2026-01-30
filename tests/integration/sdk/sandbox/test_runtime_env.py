import pytest

from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.runtime_env import NodeRuntimeEnvConfig, PythonRuntimeEnvConfig
from rock.sdk.sandbox.runtime_env.base import RuntimeEnv
from tests.integration.conftest import SKIP_IF_NO_DOCKER


async def _assert_contains(env: RuntimeEnv, cmd: str, expected: str):
    r = await env.run(cmd)
    assert expected in r.output, r.output


@pytest.mark.need_admin_and_network
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_runtime_envs(sandbox_instance: Sandbox):
    # Python: implicit default
    env = await RuntimeEnv.create(sandbox_instance, PythonRuntimeEnvConfig())
    await _assert_contains(env, "python --version", "3.11")

    # Python: explicit default
    env = await RuntimeEnv.create(sandbox_instance, PythonRuntimeEnvConfig(version="default"))
    await _assert_contains(env, "python --version", "3.11")

    # Python: pinned
    env = await RuntimeEnv.create(sandbox_instance, PythonRuntimeEnvConfig(version="3.11"))
    await _assert_contains(env, "python --version", "3.11")

    env = await RuntimeEnv.create(sandbox_instance, PythonRuntimeEnvConfig(version="3.12"))
    await _assert_contains(env, "python --version", "3.12")

    # Node: implicit default
    env = await RuntimeEnv.create(sandbox_instance, NodeRuntimeEnvConfig())
    await _assert_contains(env, "node --version", "22.18.0")

    # Node: explicit default
    env = await RuntimeEnv.create(sandbox_instance, NodeRuntimeEnvConfig(version="default"))
    await _assert_contains(env, "node --version", "22.18.0")

    # Node: pinned
    env = await RuntimeEnv.create(sandbox_instance, NodeRuntimeEnvConfig(version="22.18.0"))
    await _assert_contains(env, "node --version", "22.18.0")

    # Node: symlinks
    symlink_dir = "/usr/local/bin"
    cfg = NodeRuntimeEnvConfig(version="22.18.0", extra_symlink_dir=symlink_dir)
    await RuntimeEnv.create(sandbox_instance, cfg)

    for exe in cfg.extra_symlink_executables:
        r = await sandbox_instance.arun(f"test -L {symlink_dir}/{exe}")
        assert r.exit_code == 0, f"Symlink {exe} was not created"
