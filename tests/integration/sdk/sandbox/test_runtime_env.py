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
@pytest.mark.parametrize(
    "version,expected",
    [
        (None, "3.11"),
        ("default", "3.11"),
        ("3.11", "3.11"),
        ("3.12", "3.12"),
    ],
)
async def test_python_runtime_env_versions(sandbox_instance: Sandbox, version: str | None, expected: str):
    env = await RuntimeEnv.create(sandbox_instance, PythonRuntimeEnvConfig(version=version))
    await _assert_contains(env, "python --version", expected)


@pytest.mark.need_admin_and_network
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version,expected",
    [
        (None, "22.18.0"),
        ("default", "22.18.0"),
        ("22.18.0", "22.18.0"),
    ],
)
async def test_node_runtime_env_versions(sandbox_instance: Sandbox, version: str | None, expected: str):
    env = await RuntimeEnv.create(sandbox_instance, NodeRuntimeEnvConfig(version=version))
    await _assert_contains(env, "node --version", expected)


@pytest.mark.need_admin_and_network
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_node_runtime_env_symlinks(sandbox_instance: Sandbox):
    symlink_dir = "/usr/local/bin"
    cfg = NodeRuntimeEnvConfig(version="22.18.0", extra_symlink_dir=symlink_dir)
    await RuntimeEnv.create(sandbox_instance, cfg)

    for exe in cfg.extra_symlink_executables:
        symlink_path = f"{symlink_dir}/{exe}"
        r = await sandbox_instance.arun(f"test -L {symlink_path}")
        assert r.exit_code == 0, f"Symlink {symlink_path} was not created"
