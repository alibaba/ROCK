"""Sandbox-log archive bash command + OSS key builder.

Used by SandboxLogArchiveTask to drive archival via `runtime.execute()` —
no rocklet endpoint is added; the worker only needs `tar` and `ossutil`.

Credentials must be passed via `SandboxCommand.env` (not in the command
string), so they never appear in `ps` argv output.
"""

import shlex
import textwrap
from pathlib import Path


class ArchiveCommand:
    """Namespace for building sandbox-log archive commands and OSS keys.

    Stateless: all methods are `@staticmethod`. Grouped under a class so
    admin / CLI sides go through one explicit entry point (``ArchiveCommand.build_key``,
    ``ArchiveCommand.build_command``) and cannot drift on key layout / command shape.
    """

    @staticmethod
    def build_key(sandbox_id: str, prefix: str = "") -> str:
        """Construct the OSS object key for a sandbox-log archive.

        Layout: ``<prefix>sandbox-logs/<sandbox_id>.tar.gz``. ``prefix`` may be
        empty (flat layout under bucket root) or end with ``/``.
        """
        cleaned = (prefix or "").strip("/")
        sub = f"sandbox-logs/{sandbox_id}.tar.gz"
        return f"{cleaned}/{sub}" if cleaned else sub

    @staticmethod
    def build_command(log_dir: str, oss_key: str, bucket: str, endpoint: str) -> str:
        """Build the bash one-liner that tar+gzips ``log_dir`` and uploads to OSS.

        Why a temp-file pipeline instead of ``tar | ossutil cp -``: ossutil
        1.7.x neither reads ``OSS_ACCESS_KEY_ID`` env vars nor accepts stdin
        (``-``) as source, so we materialize the tarball under ``mktemp -d``
        and write a temporary ossutil config carrying the credentials.

        AK/SK still flow via ``OSS_ACCESS_KEY_ID`` / ``OSS_ACCESS_KEY_SECRET``
        env vars set by the caller via ``SandboxCommand.env`` — they are
        referenced by name in the command string, never substituted in, so
        ``ps`` / shell history never sees the literal values.

        ``set -e`` aborts the chain before the final ``rm -rf <log_dir>`` if
        any step fails, so the caller can rely on the exit code to retry.
        The scratch dir is removed via ``trap EXIT`` regardless of outcome.
        """
        log_path = Path(log_dir)
        parent = shlex.quote(str(log_path.parent))
        name = shlex.quote(log_path.name)
        endpoint_q = shlex.quote(endpoint)
        oss_url_q = shlex.quote(f"oss://{bucket}/{oss_key}")
        log_dir_q = shlex.quote(log_dir)
        # textwrap.dedent strips the common leading whitespace so the source can be
        # indented for readability without polluting the emitted shell string.
        return textwrap.dedent(
            f"""\
            set -e \\
            && ARCHIVE_DIR=$(mktemp -d -t sb-archive-XXXXXX) \\
            && trap 'rm -rf "$ARCHIVE_DIR"' EXIT \\
            && umask 077 \\
            && printf '[Credentials]\\nlanguage=EN\\nendpoint=%s\\naccessKeyID=%s\\naccessKeySecret=%s\\n' \\
            {endpoint_q} "$OSS_ACCESS_KEY_ID" "$OSS_ACCESS_KEY_SECRET" \\
            > "$ARCHIVE_DIR/ossconfig" \\
            && tar -czf "$ARCHIVE_DIR/archive.tar.gz" -C {parent} {name} \\
            && ossutil cp -c "$ARCHIVE_DIR/ossconfig" -f "$ARCHIVE_DIR/archive.tar.gz" {oss_url_q} \\
            && rm -rf {log_dir_q}"""
        )
