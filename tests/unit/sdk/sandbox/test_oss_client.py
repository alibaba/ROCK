"""Tests for OssClient — encapsulates all OSS operations for Sandbox."""

from rock.sdk.sandbox._oss_client import OssClient, OssClientConfig


def test_oss_client_module_imports():
    assert OssClient is not None
    assert OssClientConfig is not None
