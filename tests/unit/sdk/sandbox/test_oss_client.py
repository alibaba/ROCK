"""Tests for OssClient — encapsulates all OSS operations for Sandbox."""

import re

from rock.sdk.sandbox._oss_client import OssClient, OssClientConfig


def test_oss_client_module_imports():
    assert OssClient is not None
    assert OssClientConfig is not None


class TestComputeObjectName:
    def test_format_is_hash_dash_filename(self):
        name = OssClient._compute_object_name("sb-1", "/local/file.json", "/sandbox/file.json")
        assert re.match(r"^[0-9a-f]{64}-file\.json$", name)

    def test_deterministic_same_inputs_same_output(self):
        a = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/x")
        b = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/x")
        assert a == b

    def test_different_sandbox_id_yields_different_hash(self):
        a = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/x")
        b = OssClient._compute_object_name("sb-2", "/local/x", "/sandbox/x")
        assert a != b

    def test_different_local_path_yields_different_hash(self):
        a = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/x")
        b = OssClient._compute_object_name("sb-1", "/local/y", "/sandbox/x")
        assert a != b

    def test_different_sandbox_path_yields_different_hash(self):
        a = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/x")
        b = OssClient._compute_object_name("sb-1", "/local/x", "/sandbox/y")
        assert a != b

    def test_filename_is_basename_of_local_path(self):
        name = OssClient._compute_object_name("sb-1", "/dir1/dir2/foo.txt", "/other/bar.txt")
        assert name.endswith("-foo.txt")

    def test_filename_falls_back_to_sandbox_path_basename_when_local_empty(self):
        name = OssClient._compute_object_name("sb-1", "", "/sandbox/baz.txt")
        assert name.endswith("-baz.txt")
