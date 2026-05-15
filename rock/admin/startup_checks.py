from rock.config import RockConfig
from rock.deployments.log_cleanup import LogCleanupPolicy
from rock.logger import init_logger

logger = init_logger(__name__)


def check_oss_consistency_with_log_policy(rock_config: RockConfig) -> None:
    """Startup-time consistency checks for admin service.

    These run once when admin starts and emit WARN logs but do NOT
    abort startup — the runtime fail-safe handles the actual risk
    (OssArchiver returns False on missing primary config, scheduler
    task then preserves the dir; FileCleanupTask is the janitor).
    """
    policy = rock_config.sandbox_config.sandbox_log_cleanup_policy_default
    primary = rock_config.oss.primary
    if policy == LogCleanupPolicy.KEEP_THEN_ARCHIVE and not primary.bucket:
        logger.warning(
            "sandbox_log_cleanup_policy_default=keep_then_archive but "
            "OssConfig.primary.bucket is empty. SandboxLogArchiveTask will "
            "fail every cycle and dirs will pile up on workers until "
            "FileCleanupTask purges them by mtime "
            "(default 7 days). Either configure oss.primary.* or change "
            "the policy default to 'keep' (relies solely on FileCleanupTask) "
            "or 'clean_directly' (accepts permanent loss)."
        )
