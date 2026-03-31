"""Tests for sandbox speedup functionality."""

from urllib.parse import urlparse

import pytest

from rock.actions import Command
from rock.logger import init_logger
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.speedup import SpeedupType
from tests.integration.conftest import SKIP_IF_NO_DOCKER

logger = init_logger(__name__)


async def _assert_speedup_apt(sandbox: Sandbox):
    logger.info("Testing APT public mirror configuration...")
    result = await sandbox.network.speedup(
        speedup_type=SpeedupType.APT,
        speedup_value="http://mirrors.cloud.aliyuncs.com",
    )
    assert result.exit_code == 0, f"APT public mirror failed: {result.output}"
    logger.info("APT public mirror configured successfully")

    logger.info("Verifying /etc/apt/sources.list content...")
    check_result = await sandbox.execute(Command(command=["cat", "/etc/apt/sources.list"]))
    assert check_result.exit_code == 0, "Failed to read /etc/apt/sources.list"
    sources_content = check_result.stdout

    # Extract URLs from sources.list and verify the mirror host via parsed hostname
    source_urls = [word for line in sources_content.splitlines() for word in line.split() if word.startswith("http")]
    mirror_hosts = {urlparse(url).hostname for url in source_urls}
    assert (
        "mirrors.cloud.aliyuncs.com" in mirror_hosts
    ), f"Mirror hostname not found in sources.list URLs. Content:\n{sources_content}"
    logger.info(f"APT sources.list verified successfully:\n{sources_content}")


async def _assert_speedup_pip(sandbox: Sandbox):
    logger.info("Testing PIP mirror (http)...")
    result = await sandbox.network.speedup(
        speedup_type=SpeedupType.PIP,
        speedup_value="http://mirrors.cloud.aliyuncs.com",
    )
    assert result.exit_code == 0, f"PIP mirror failed: {result.output}"
    logger.info("PIP mirror configured successfully")

    logger.info("Verifying /root/.pip/pip.conf content...")
    check_result = await sandbox.execute(Command(command=["cat", "/root/.pip/pip.conf"]))
    assert check_result.exit_code == 0, "Failed to read /root/.pip/pip.conf"
    pip_config_content = check_result.stdout

    # Verify PIP mirror by parsing the index-url hostname
    pip_urls = [word for line in pip_config_content.splitlines() for word in line.split() if word.startswith("http")]
    pip_hosts = {urlparse(url).hostname for url in pip_urls}
    assert (
        "mirrors.cloud.aliyuncs.com" in pip_hosts
    ), f"PIP mirror hostname not found in pip.conf. Content:\n{pip_config_content}"
    assert any(
        line.strip().startswith("trusted-host")
        and urlparse(f"http://{line.split('=', 1)[1].strip()}").hostname == "mirrors.cloud.aliyuncs.com"
        for line in pip_config_content.splitlines()
        if "=" in line
    ), f"trusted-host not found in pip.conf. Content:\n{pip_config_content}"
    logger.info(f"PIP config verified successfully:\n{pip_config_content}")

    logger.info("Testing PIP mirror (https)...")
    result = await sandbox.network.speedup(
        speedup_type=SpeedupType.PIP,
        speedup_value="https://mirrors.aliyun.com",
    )
    assert result.exit_code == 0, f"PIP aliyun mirror failed: {result.output}"
    logger.info("PIP aliyun mirror configured successfully")

    check_result = await sandbox.execute(Command(command=["cat", "/root/.pip/pip.conf"]))
    assert check_result.exit_code == 0, "Failed to read /root/.pip/pip.conf after updating"
    pip_config_content = check_result.stdout
    updated_pip_urls = [
        word for line in pip_config_content.splitlines() for word in line.split() if word.startswith("http")
    ]
    updated_pip_hosts = {urlparse(url).hostname for url in updated_pip_urls}
    assert (
        "mirrors.aliyun.com" in updated_pip_hosts
    ), f"Updated PIP mirror hostname not found. Content:\n{pip_config_content}"


async def _assert_speedup_github(sandbox: Sandbox):
    logger.info("Testing GitHub acceleration...")
    result = await sandbox.network.speedup(
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
