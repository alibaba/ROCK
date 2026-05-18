"""Tests for rock.utils.archive_command."""

from rock.utils.archive_command import ArchiveCommand


class TestArchiveCommandBuildKey:
    def test_with_prefix(self):
        assert ArchiveCommand.build_key("sb-123", "rock-archives/") == "rock-archives/sandbox-logs/sb-123.tar.gz"

    def test_strips_leading_and_trailing_slashes(self):
        # Avoid double slashes regardless of how the prefix is configured in YAML.
        assert ArchiveCommand.build_key("sb-1", "/rock-archives//") == "rock-archives/sandbox-logs/sb-1.tar.gz"

    def test_empty_prefix_yields_flat_layout(self):
        assert ArchiveCommand.build_key("sb-1") == "sandbox-logs/sb-1.tar.gz"
        assert ArchiveCommand.build_key("sb-1", "") == "sandbox-logs/sb-1.tar.gz"

    def test_no_double_slash_at_join_boundary(self):
        # Strip only guards against leading/trailing slash duplication; internal
        # slashes are preserved as-is (caller's responsibility to pass a sane prefix).
        assert ArchiveCommand.build_key("sb-1", "/a/b/").startswith("a/b/sandbox-logs/")


class TestArchiveCommandBuildCommand:
    def test_starts_with_set_e_so_failures_short_circuit(self):
        # `set -e` is what guarantees the trailing `rm -rf <log_dir>` is
        # skipped on tar / ossutil failure, so retry can re-archive cleanly.
        # Triple-quoted multi-line string emits `set -e \\\n&& ...` (bash treats
        # `\<newline>` as line continuation, so it's still one chained command).
        cmd = ArchiveCommand.build_command("/data/logs/sb-1", "k", "b", "e")
        assert cmd.startswith("set -e \\\n&&")

    def test_uses_mktemp_and_traps_exit_for_cleanup(self):
        # ossutil 1.7.x cannot read stdin, so we must land a temp tarball;
        # mktemp -d isolates concurrent archives, trap EXIT cleans up.
        cmd = ArchiveCommand.build_command("/data/logs/sb-1", "k", "b", "e")
        assert "mktemp -d" in cmd
        assert "trap " in cmd and "EXIT" in cmd

    def test_writes_ossutil_config_with_endpoint_and_env_var_credentials(self):
        # ossutil 1.7.x ignores OSS_ACCESS_KEY_* env vars, so we materialize
        # an [Credentials] config file. AK/SK are still passed by env-var
        # reference (printf "%s" + "$OSS_ACCESS_KEY_*"), never substituted.
        cmd = ArchiveCommand.build_command("/data/logs/sb-1", "k", "b", "oss-cn-hangzhou.aliyuncs.com")
        assert "[Credentials]" in cmd
        assert "language=EN" in cmd
        assert "endpoint=%s" in cmd
        assert "accessKeyID=%s" in cmd
        assert "accessKeySecret=%s" in cmd
        assert (
            "oss-cn-hangzhou.aliyuncs.com" in cmd
        )  # endpoint passed through (shlex.quote skips quoting for safe chars)
        assert '"$OSS_ACCESS_KEY_ID"' in cmd
        assert '"$OSS_ACCESS_KEY_SECRET"' in cmd

    def test_tar_then_ossutil_cp_against_temp_files(self):
        cmd = ArchiveCommand.build_command("/data/logs/sb-1", "k", "b", "e")
        # tar produces a real file under the scratch dir.
        assert 'tar -czf "$ARCHIVE_DIR/archive.tar.gz"' in cmd
        # ossutil cp uses -c <config> -f <file> <oss_url> (1.7.x compatible).
        assert 'ossutil cp -c "$ARCHIVE_DIR/ossconfig" -f "$ARCHIVE_DIR/archive.tar.gz"' in cmd

    def test_uses_parent_dir_for_tar_so_archive_does_not_embed_full_path(self):
        cmd = ArchiveCommand.build_command("/data/logs/sb-1", "k", "b", "e")
        # `-C <parent> <basename>` keeps the tarball flat at <basename>/, not
        # /data/logs/sb-1/ — important for restore.
        assert "-C /data/logs sb-1" in cmd

    def test_paths_are_shell_quoted(self):
        cmd = ArchiveCommand.build_command(
            log_dir="/data/logs/has space/sb-1",
            oss_key="rock-archives/has space/sb-1.tar.gz",
            bucket="b",
            endpoint="e",
        )
        # shlex.quote wraps anything with spaces in single quotes
        assert "'/data/logs/has space/sb-1'" in cmd
        assert "'/data/logs/has space'" in cmd

    def test_oss_url_is_built_from_bucket_and_key(self):
        cmd = ArchiveCommand.build_command(
            "/data/logs/sb-1", "rock-archives/sandbox-logs/sb-1.tar.gz", "chatos-rock", "e"
        )
        assert "oss://chatos-rock/rock-archives/sandbox-logs/sb-1.tar.gz" in cmd

    def test_log_dir_rm_is_last_and_chained_on_success(self):
        # The final rm is gated by the && chain — set -e + && ensures it
        # never runs if any prior step failed.
        cmd = ArchiveCommand.build_command("/data/logs/sb-1", "k", "b", "e")
        assert cmd.endswith("&& rm -rf /data/logs/sb-1")

    def test_no_literal_credentials_in_command_string(self):
        # AK/SK appear ONLY as env-var references; never as literals or
        # ossutil --access-key flag (which would expose them in argv).
        cmd = ArchiveCommand.build_command("/data/logs/sb-1", "k", "b", "e")
        assert '"$OSS_ACCESS_KEY_ID"' in cmd
        assert '"$OSS_ACCESS_KEY_SECRET"' in cmd
        assert "--access-key" not in cmd
        # `LTAI` is the prefix of every alibaba live AK — cheap regression check.
        assert "LTAI" not in cmd
