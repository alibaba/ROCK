import pytest

from rock.actions import Command
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
    node_symlink_dir = "/usr/local/bin"
    cfg = NodeRuntimeEnvConfig(extra_symlink_dir=node_symlink_dir)
    await RuntimeEnv.create(sandbox_instance, cfg)

    for exe in cfg.extra_symlink_executables:
        # check ["node", "npm", "npx"]
        r = await sandbox_instance.execute(Command(command=["test", "-L", f"{node_symlink_dir}/{exe}"]))
        assert r.exit_code == 0, f"Symlink {exe} was not created"

    # Python: symlinks
    python_symlink_dir = "/tmp"
    cfg = PythonRuntimeEnvConfig(extra_symlink_dir=python_symlink_dir)
    await RuntimeEnv.create(sandbox_instance, cfg)

    for exe in cfg.extra_symlink_executables:
        # check ["python", "python3", "pip", "pip3"]
        r = await sandbox_instance.execute(Command(command=["test", "-L", f"{python_symlink_dir}/{exe}"]))
        assert r.exit_code == 0, f"Symlink {exe} was not created"
