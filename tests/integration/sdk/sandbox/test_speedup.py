"""Tests for sandbox speedup functionality."""

import pytest

from rock.actions import Command
from rock.logger import init_logger
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.speedup import SpeedupType
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = init_logger(__name__)


async def _assert_speedup_apt(sandbox: Sandbox):
    logger.info("Testing APT public mirror configuration...")
    result = await sandbox.speedup(
        speedup_type=SpeedupType.APT,
        speedup_value="http://mirrors.cloud.aliyuncs.com",
    )
    assert result.exit_code == 0, f"APT public mirror failed: {result.output}"
    logger.info("APT public mirror configured successfully")

    logger.info("Verifying /etc/apt/sources.list content...")
    check_result = await sandbox.execute(Command(command=["cat", "/etc/apt/sources.list"]))
    assert check_result.exit_code == 0, "Failed to read /etc/apt/sources.list"
    sources_content = check_result.stdout

    assert (
        "mirrors.cloud.aliyuncs.com" in sources_content
    ), f"Mirror URL not found in sources.list. Content:\n{sources_content}"
    assert (
        "deb http://mirrors.cloud.aliyuncs.com" in sources_content
    ), f"Expected deb entry not found. Content:\n{sources_content}"
    logger.info(f"APT sources.list verified successfully:\n{sources_content}")


async def _assert_speedup_pip(sandbox: Sandbox):
    logger.info("Testing PIP mirror (http)...")
    result = await sandbox.speedup(
        speedup_type=SpeedupType.PIP,
        speedup_value="http://mirrors.cloud.aliyuncs.com",
    )
    assert result.exit_code == 0, f"PIP mirror failed: {result.output}"
    logger.info("PIP mirror configured successfully")

    logger.info("Verifying /root/.pip/pip.conf content...")
    check_result = await sandbox.execute(Command(command=["cat", "/root/.pip/pip.conf"]))
    assert check_result.exit_code == 0, "Failed to read /root/.pip/pip.conf"
    pip_config_content = check_result.stdout

    assert (
        "mirrors.cloud.aliyuncs.com/pypi/simple/" in pip_config_content
    ), f"PIP mirror URL not found in pip.conf. Content:\n{pip_config_content}"
    assert (
        "trusted-host = mirrors.cloud.aliyuncs.com" in pip_config_content
    ), f"trusted-host not found in pip.conf. Content:\n{pip_config_content}"
    logger.info(f"PIP config verified successfully:\n{pip_config_content}")

    logger.info("Testing PIP mirror (https)...")
    result = await sandbox.speedup(
        speedup_type=SpeedupType.PIP,
        speedup_value="https://mirrors.aliyun.com",
    )
    assert result.exit_code == 0, f"PIP aliyun mirror failed: {result.output}"
    logger.info("PIP aliyun mirror configured successfully")

    check_result = await sandbox.execute(Command(command=["cat", "/root/.pip/pip.conf"]))
    assert check_result.exit_code == 0, "Failed to read /root/.pip/pip.conf after updating"
    pip_config_content = check_result.stdout
    assert (
        "mirrors.aliyun.com/pypi/simple/" in pip_config_content
    ), f"Updated PIP mirror URL not found. Content:\n{pip_config_content}"


async def _assert_speedup_github(sandbox: Sandbox):
    logger.info("Testing GitHub acceleration...")
    result = await sandbox.speedup(
        speedup_type=SpeedupType.GITHUB,
        speedup_value="11.11.11.11",
    )
    assert result.exit_code == 0, f"GitHub acceleration failed: {result.output}"
    logger.info("GitHub acceleration configured successfully")

    logger.info("Verifying /etc/hosts content...")
    check_result = await sandbox.execute(Command(command=["cat", "/etc/hosts"]))
    assert check_result.exit_code == 0, "Failed to read /etc/hosts"
    hosts_content = check_result.stdout

    assert "11.11.11.11 github.com" in hosts_content, f"Updated GitHub IP not found. Content:\n{hosts_content}"
    logger.info(f"GitHub IP update verified successfully:\n{hosts_content}")


@pytest.mark.need_admin
@SKIP_IF_NO_DOCKER
@pytest.mark.asyncio
async def test_sandbox_speedup_all_in_one(sandbox_instance: Sandbox):
    """Run all speedup checks in one sandbox."""
    await _assert_speedup_apt(sandbox_instance)
    await _assert_speedup_pip(sandbox_instance)
    await _assert_speedup_github(sandbox_instance)
