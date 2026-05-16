"""Tests for rock.utils.archive_command."""

from rock.utils.archive_command import build_archive_command, build_sandbox_log_key


class TestBuildSandboxLogKey:
    def test_with_prefix(self):
        assert build_sandbox_log_key("sb-123", "rock-archives/") == "rock-archives/sandbox-logs/sb-123.tar.gz"

    def test_strips_leading_and_trailing_slashes(self):
        # Avoid double slashes regardless of how the prefix is configured in YAML.
        assert build_sandbox_log_key("sb-1", "/rock-archives//") == "rock-archives/sandbox-logs/sb-1.tar.gz"

    def test_empty_prefix_yields_flat_layout(self):
        assert build_sandbox_log_key("sb-1") == "sandbox-logs/sb-1.tar.gz"
        assert build_sandbox_log_key("sb-1", "") == "sandbox-logs/sb-1.tar.gz"

    def test_no_double_slash_at_join_boundary(self):
        # Strip only guards against leading/trailing slash duplication; internal
        # slashes are preserved as-is (caller's responsibility to pass a sane prefix).
        assert build_sandbox_log_key("sb-1", "/a/b/").startswith("a/b/sandbox-logs/")


class TestBuildArchiveCommand:
    def test_contains_tar_pipe_ossutil_then_rm(self):
        cmd = build_archive_command(
            log_dir="/data/logs/sb-1",
            oss_key="rock-archives/sandbox-logs/sb-1.tar.gz",
            bucket="chatos-rock",
            endpoint="oss-cn-hangzhou.aliyuncs.com",
        )
        # tar streams to ossutil, only rm on success (&& chains)
        assert "tar -czf -" in cmd
        assert "| ossutil cp -f -" in cmd
        assert "&& rm -rf" in cmd

    def test_uses_parent_dir_for_tar_so_archive_does_not_embed_full_path(self):
        cmd = build_archive_command("/data/logs/sb-1", "k", "b", "e")
        # `-C <parent> <basename>` keeps the tarball flat at <basename>/, not
        # /data/logs/sb-1/ — important for restore.
        assert "-C /data/logs sb-1" in cmd

    def test_paths_are_shell_quoted(self):
        cmd = build_archive_command(
            log_dir="/data/logs/has space/sb-1",
            oss_key="rock-archives/has space/sb-1.tar.gz",
            bucket="b",
            endpoint="e",
        )
        # shlex.quote wraps anything with spaces in single quotes
        assert "'/data/logs/has space/sb-1'" in cmd
        assert "'/data/logs/has space'" in cmd

    def test_oss_url_is_built_from_bucket_and_key(self):
        cmd = build_archive_command("/data/logs/sb-1", "rock-archives/sandbox-logs/sb-1.tar.gz", "chatos-rock", "e")
        assert "oss://chatos-rock/rock-archives/sandbox-logs/sb-1.tar.gz" in cmd

    def test_no_credentials_in_command_string(self):
        # AK/SK MUST flow via SandboxCommand.env, never the command string.
        cmd = build_archive_command("/data/logs/sb-1", "k", "b", "e")
        assert "ACCESS_KEY" not in cmd.upper()
        assert "SECRET" not in cmd.upper()
        assert "--access-key" not in cmd
