import asyncio
import io
import os
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

import oss2

from rock.admin.metrics.constants import MetricsConstants
from rock.admin.metrics.monitor import MetricsMonitor
from rock.config import RockConfig
from rock.logger import init_logger

logger = init_logger(__name__)


_MEMORY_BUFFER_MAX = 100 * 1024 * 1024  # 100MB
_DEFAULT_MAX_SIZE = 5 * 1024 * 1024 * 1024  # 5GB
_DEFAULT_TIMEOUT_SECONDS = 60

# Dedicated thread pool for tar+upload work. Decoupled from
# DockerDeployment.stop()'s stop_executor so they don't compete.
# max_workers=4 caps concurrent archives across the admin process;
# additional calls queue, but the per-call timeout ensures the queue
# does not grow unbounded
_archive_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="oss-archive",
)


class OssArchiver:
    """tar+gzip a local directory and upload to OSS primary bucket.

    Design choices (vs PR-1 v1):
    - read oss.primary.* (not top-level oss.*)
    - no _bucket class singleton (re-created per call; nacos-friendly,
      easier to mock in unit tests)
    - upload via long-term AK/SK (not STS), since archiver may run in
      scheduler task whose tick can exceed an STS token lifetime
    """

    _archive_prefix: str = "rock-archives/"
    _archive_ttl_days: int = 30

    @classmethod
    def build_sandbox_log_key(cls, container_name: str) -> str:
        oss = RockConfig.from_env().oss
        prefix = (oss.archive_prefix or "rock-archives/").rstrip("/") + "/"
        return f"{prefix}sandbox-logs/{container_name}.tar.gz"

    @classmethod
    def _get_bucket(cls) -> oss2.Bucket | None:
        """read from oss.primary, no class-level cache."""
        try:
            primary = RockConfig.from_env().oss.primary
            if not primary.bucket:
                return None
            auth = oss2.Auth(primary.access_key_id, primary.access_key_secret)
            return oss2.Bucket(auth, primary.endpoint, primary.bucket)
        except Exception as e:
            logger.warning(f"OSS primary bucket init failed: {e}")
            return None

    @classmethod
    def _sync_prefix_from_config(cls) -> None:
        """Refresh archive_prefix + ttl from RockConfig.

        Called per-key-build so config changes take effect on next
        archive without process restart (cheap; ~1µs).
        """
        try:
            oss = RockConfig.from_env().oss
            cls._archive_prefix = (oss.archive_prefix or "rock-archives/").rstrip("/") + "/"
            cls._archive_ttl_days = int(oss.archive_ttl_days or 30)
        except Exception as e:
            logger.warning(f"Failed to sync archive prefix from config: {e}")

    @classmethod
    def _dir_size_bytes(cls, local_dir: str) -> int:
        total = 0
        for root, _, files in os.walk(local_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass  # file disappeared mid-walk; ignore
        return total

    @classmethod
    def try_upload_dir_sync(
        cls,
        local_dir: str,
        oss_key: str,
        *,
        container_name: str | None = None,
        metrics_monitor: MetricsMonitor | None = None,
        max_size_bytes: int = _DEFAULT_MAX_SIZE,
        compression_level: int = 6,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> bool:
        """Sync tar+gzip upload with hard timeout, safe to call from _stop().

        oss_key MUST be built via build_sandbox_log_key() (or future
        sibling builders) to ensure it lives under archive_prefix.

        Args:
            metrics_monitor: optional MetricsMonitor instance for
                recording archive attempt/success/failure/size metrics.
                Caller (e.g. DockerDeployment._archive_then_clean)
                passes its own self.metrics_monitor here to keep
                attribution consistent. None = silent (unit tests,
                or callers that don't care about metrics).

        Returns:
            True  on successful upload (or empty / missing dir).
            False on init failure / size limit / IO / network error /
                  timeout. Caller MUST treat False as "do not delete
                  the local dir".

        Metrics emitted (when metrics_monitor is provided):
            sandbox.log.archive.total    — every call
            sandbox.log.archive.success  — on True
            sandbox.log.archive.failure  — on False
            sandbox.log.archive.size_bytes — gauge of raw size on success
        """
        attrs = {"container": container_name or "unknown"}

        if metrics_monitor:
            metrics_monitor.record_counter_by_name(
                MetricsConstants.SANDBOX_LOG_ARCHIVE_TOTAL,
                1,
                attrs,
            )

        future = _archive_executor.submit(
            cls._upload_blocking,
            local_dir,
            oss_key,
            max_size_bytes,
            compression_level,
            metrics_monitor,
            attrs,
        )
        try:
            ok = future.result(timeout=timeout_seconds)
        except FuturesTimeoutError:
            logger.warning(
                f"OSS archive timed out after {timeout_seconds}s for "
                f"{local_dir}; preserving local dir. The worker thread "
                f"may still run in background — investigate network "
                f"or OSS endpoint health."
            )
            ok = False
        except Exception as e:
            logger.exception(f"OSS archive submission failed for {local_dir}: {e}")
            ok = False

        if metrics_monitor:
            metric_name = (
                MetricsConstants.SANDBOX_LOG_ARCHIVE_SUCCESS if ok else MetricsConstants.SANDBOX_LOG_ARCHIVE_FAILURE
            )
            metrics_monitor.record_counter_by_name(metric_name, 1, attrs)
        return ok

    @classmethod
    def _upload_blocking(
        cls,
        local_dir: str,
        oss_key: str,
        max_size_bytes: int,
        compression_level: int,
        metrics_monitor: MetricsMonitor | None,
        attrs: dict,
    ) -> bool:
        """Blocking implementation; runs in _archive_executor."""
        cls._sync_prefix_from_config()
        bucket = cls._get_bucket()
        if not bucket:
            return False

        if not os.path.isdir(local_dir):
            logger.info(f"Archive skipped, dir does not exist: {local_dir}")
            return True

        total = cls._dir_size_bytes(local_dir)
        if total == 0:
            logger.info(f"Archive skipped, dir is empty: {local_dir}")
            return True
        if total > max_size_bytes:
            logger.warning(
                f"Skip archive {local_dir}: {total} bytes > limit "
                f"{max_size_bytes}; keeping raw logs (see oss_archiver "
                f"docstring section 'Why the 5GB ceiling?')"
            )
            return False

        headers = {
            "x-oss-meta-ttl-days": str(cls._archive_ttl_days),
            "x-oss-meta-original-size": str(total),
            "Content-Type": "application/gzip",
        }
        arcname = os.path.basename(local_dir.rstrip("/")) or "archive"

        try:
            if total < _MEMORY_BUFFER_MAX:
                buf = io.BytesIO()
                with tarfile.open(
                    fileobj=buf,
                    mode="w:gz",
                    compresslevel=compression_level,
                ) as tar:
                    tar.add(local_dir, arcname=arcname)
                compressed = buf.tell()
                buf.seek(0)
                bucket.put_object(oss_key, buf, headers=headers)
            else:
                with tempfile.NamedTemporaryFile(
                    suffix=".tar.gz",
                    delete=True,
                ) as tmp:
                    with tarfile.open(
                        tmp.name,
                        mode="w:gz",
                        compresslevel=compression_level,
                    ) as tar:
                        tar.add(local_dir, arcname=arcname)
                    compressed = os.path.getsize(tmp.name)
                    bucket.put_object_from_file(
                        oss_key,
                        tmp.name,
                        headers=headers,
                    )

            ratio = total / compressed if compressed else 0
            logger.info(
                f"Archived {local_dir} -> oss://{bucket.bucket_name}/{oss_key} "
                f"(raw={total}B compressed={compressed}B ratio={ratio:.1f}x "
                f"ttl_days={cls._archive_ttl_days})"
            )
            if metrics_monitor:
                metrics_monitor.record_gauge_by_name(
                    MetricsConstants.SANDBOX_LOG_ARCHIVE_SIZE,
                    float(total),
                    attrs,
                )
            return True

        except Exception as e:
            logger.exception(f"OSS archive blocking call failed for {local_dir}: {e}")
            return False

    @classmethod
    async def get_object(cls, oss_key: str, local_path: str) -> bool:
        """Async download for `rock storage get` CLI."""
        bucket = cls._get_bucket()
        if not bucket:
            return False
        try:
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            await asyncio.to_thread(
                bucket.get_object_to_file,
                oss_key,
                local_path,
            )
            logger.info(f"Downloaded oss://.../{oss_key} -> {local_path}")
            return True
        except Exception:
            logger.exception(f"OSS download failed for {oss_key}")
            return False
