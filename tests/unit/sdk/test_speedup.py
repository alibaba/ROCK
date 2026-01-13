"""Tests for sandbox speedup functionality."""

import pytest

from rock.logger import init_logger

logger = init_logger(__name__)


@pytest.mark.asyncio
async def test_apt_url_parsing():
    """Test APT mirror URL parsing and normalization."""
    from rock.sdk.sandbox.speedup.strategies.apt import AptSpeedupStrategy

    strategy = AptSpeedupStrategy()

    # HTTP URL without trailing slash
    result = strategy.parse_value("http://mirrors.cloud.aliyuncs.com")
    assert result["mirror_base"] == "http://mirrors.cloud.aliyuncs.com"

    # HTTPS URL with trailing slash should be normalized
    result = strategy.parse_value("https://mirrors.aliyun.com/")
    assert result["mirror_base"] == "https://mirrors.aliyun.com"

    # URL with custom path should be preserved
    result = strategy.parse_value("http://custom.net/mirrors")
    assert result["mirror_base"] == "http://custom.net/mirrors"

    logger.info("APT URL parsing tests passed")


@pytest.mark.asyncio
async def test_pip_url_parsing():
    """Test PIP mirror URL parsing and index path generation."""
    from rock.sdk.sandbox.speedup.strategies.pip import PipSpeedupStrategy

    strategy = PipSpeedupStrategy()

    # HTTP domain should generate correct index URL and extract trusted host
    result = strategy.parse_value("http://mirrors.cloud.aliyuncs.com")
    assert result["pip_index_url"] == "http://mirrors.cloud.aliyuncs.com/pypi/simple/"
    assert result["pip_trusted_host"] == "mirrors.cloud.aliyuncs.com"

    # HTTPS domain with different host
    result = strategy.parse_value("https://mirrors.aliyun.com")
    assert result["pip_index_url"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert result["pip_trusted_host"] == "mirrors.aliyun.com"

    # URL with custom path should append index path correctly
    result = strategy.parse_value("https://fake-url.com/1")
    assert result["pip_index_url"] == "https://fake-url.com/1/pypi/simple/"
    assert result["pip_trusted_host"] == "fake-url.com"

    # Trailing slash should be normalized before appending index path
    result = strategy.parse_value("http://mirrors.cloud.aliyuncs.com/")
    assert result["pip_index_url"] == "http://mirrors.cloud.aliyuncs.com/pypi/simple/"
    assert result["pip_trusted_host"] == "mirrors.cloud.aliyuncs.com"

    logger.info("PIP URL parsing tests passed")


@pytest.mark.asyncio
async def test_github_ip_parsing():
    """Test GitHub IP address validation and hosts entry generation."""
    from rock.sdk.sandbox.speedup.strategies.github import GithubSpeedupStrategy

    strategy = GithubSpeedupStrategy()

    # Valid IP address should generate correct hosts entry
    result = strategy.parse_value("11.11.11.11")
    assert result["hosts_entry"] == "11.11.11.11 github.com"

    # Non-numeric format should raise ValueError
    with pytest.raises(ValueError, match="Invalid IP address format"):
        strategy.parse_value("invalid.ip.address")

    # IP octet out of valid range (0-255) should raise ValueError
    with pytest.raises(ValueError, match="octet value must be 0-255"):
        strategy.parse_value("256.1.1.1")

    logger.info("GitHub IP parsing tests passed")
