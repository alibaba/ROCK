from enum import Enum

# Sentinel filename written by DockerDeployment._stop() in
# ${ROCK_LOGGING_PATH}/<container_name>/. Presence + JSON content
# drives the deferred-archive scheduler task.
LOG_STOPPED_SENTINEL = ".rock_stopped_at"


class LogCleanupPolicy(str, Enum):
    """Per-sandbox bind-mount log directory cleanup policy on _stop().

    Applies ONLY to the per-sandbox UUID dir under ROCK_LOGGING_PATH,
    NOT to host-side logs (/data/logs/*.log) which are managed by
    logrotate.
    """

    KEEP = "keep"
    """Do not touch the dir. Relies on FileCleanupTask to purge file
    contents by mtime; the empty dir shell may persist."""

    KEEP_THEN_ARCHIVE = "keep_then_archive"
    """Default. _stop() only writes a sentinel file marking when the
    sandbox stopped; the SandboxLogArchiveTask scheduler task picks
    up the dir after `oss.keep_days_before_archive` days, tar+gzips
    it to OSS, and removes the dir on success.

    Failure mode is fail-safe: after `oss.archive_max_attempts`
    failures the dir is PRESERVED (never silently destroyed) and
    `sandbox.log.archive.failed_persist` counter is emitted.
    FileCleanupTask is the eventual janitor (`max_age_mins` triggers
    file deletion by mtime) — operators get a window to investigate
    the OSS issue before any data is lost.
    """

    CLEAN_DIRECTLY = "clean_directly"
    """Delete without archiving. Caller explicitly accepts permanent
    loss. Use when no OSS budget and operator manually offloaded any
    needed logs upfront."""
