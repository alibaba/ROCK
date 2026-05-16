"""Pure functions for building sandbox-log archive bash commands and OSS keys.

Used by SandboxLogArchiveTask to drive archival via `runtime.execute()` —
no rocklet endpoint is added; the worker only needs `tar` and `ossutil`.

Credentials must be passed via `SandboxCommand.env` (not in the command
string), so they never appear in `ps` argv output.
"""

import shlex
from pathlib import Path


def build_sandbox_log_key(sandbox_id: str, prefix: str = "") -> str:
    """Construct the OSS object key for a sandbox-log archive.

    Layout: ``<prefix>sandbox-logs/<sandbox_id>.tar.gz``. ``prefix`` may be
    empty (flat layout under bucket root) or end with ``/``.
    """
    cleaned = (prefix or "").strip("/")
    sub = f"sandbox-logs/{sandbox_id}.tar.gz"
    return f"{cleaned}/{sub}" if cleaned else sub


def build_archive_command(log_dir: str, oss_key: str, bucket: str, endpoint: str) -> str:
    """Build the bash one-liner that tar+gzips ``log_dir`` and streams to OSS.

    On non-zero exit (tar / ossutil failure), the trailing ``rm -rf`` is
    skipped — caller relies on the exit code to decide retry vs delete.
    AK/SK come from ``OSS_ACCESS_KEY_ID`` / ``OSS_ACCESS_KEY_SECRET`` env
    vars set by the caller via ``SandboxCommand.env``.
    """
    log_path = Path(log_dir)
    parent = str(log_path.parent)
    name = log_path.name
    oss_url = f"oss://{bucket}/{oss_key}"
    return (
        f"tar -czf - -C {shlex.quote(parent)} {shlex.quote(name)} "
        f"| ossutil cp -f - {shlex.quote(oss_url)} --endpoint {shlex.quote(endpoint)} "
        f"&& rm -rf {shlex.quote(log_dir)}"
    )
