# rock/admin/scheduler/tasks/image_pull_task.py
from __future__ import annotations

from dataclasses import dataclass

from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import PID_PREFIX, PID_SUFFIX, SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.utils.docker import ImageUtil
from rock.utils.system import extract_nohup_pid

logger = init_logger(name="image_pull", file_name=SCHEDULER_LOG_NAME)


@dataclass
class ImageEntry:
    """A single image to pull, with optional per-image registry credentials."""

    image: str
    registry_username: str | None = None
    registry_password: str | None = None  # base64-encoded

    @classmethod
    def from_raw(cls, raw: str | dict) -> ImageEntry:
        """Parse an image entry from YAML config.

        Accepts either a plain string (no auth) or a dict with image/username/password.
        """
        if isinstance(raw, str):
            return cls(image=raw)
        if isinstance(raw, dict):
            return cls(
                image=raw["image"],
                registry_username=raw.get("registry_username"),
                registry_password=raw.get("registry_password"),
            )
        raise ValueError(f"Invalid image entry: {raw!r}, expected str or dict")

    @property
    def needs_login(self) -> bool:
        return bool(self.registry_username and self.registry_password)

    @property
    def registry(self) -> str:
        """Extract registry host from image name (e.g. 'registry.example.com')."""
        registry, _ = ImageUtil.parse_registry_and_others(self.image)
        return registry


class ImagePullTask(BaseTask):
    """Scheduled task for pre-pulling Docker images on all workers.

    Reads a list of images from YAML config and runs `docker pull` for each image
    on every alive worker node. This ensures images are locally available before
    sandbox creation, reducing cold-start latency.

    Each image can optionally carry its own registry credentials (username + base64-
    encoded password). If credentials are provided, `docker login <registry>` is
    executed before pulling that image.

    The task is idempotent — `docker pull` is safe to repeat: if the image already
    exists locally and is up-to-date, the pull is a no-op.
    """

    def __init__(
        self,
        interval_seconds: int = 3600,
        image_entries: list[ImageEntry] | None = None,
    ):
        """Initialize image pull task.

        Args:
            interval_seconds: Execution interval in seconds, default 1 hour
            image_entries: List of ImageEntry objects to pull
        """
        super().__init__(
            type="image_pull",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.IDEMPOTENT,
        )
        self.image_entries = image_entries or []

    @classmethod
    def from_config(cls, task_config) -> ImagePullTask:
        """Create task instance from config.

        Expected config params:
            images: list — Each item is either a plain image string (no auth)
                          or a dict with image/registry_username/registry_password.

        Example YAML:
            scheduler:
              tasks:
                - task_class: rock.admin.scheduler.tasks.image_pull_task.ImagePullTask
                  enabled: true
                  interval_seconds: 3600
                  params:
                    images:
                      - image: "my-registry.example.com/chatos/python:313"
                        registry_username: "myuser"
                        registry_password: "bXlwYXNzd29yZA=="  # base64-encoded
                      - "ubuntu:22.04"  # no auth needed
        """
        raw_images = task_config.params.get("images", [])
        image_entries = [ImageEntry.from_raw(item) for item in raw_images]
        return cls(
            interval_seconds=task_config.interval_seconds,
            image_entries=image_entries,
        )

    def _build_login_command(self, entry: ImageEntry) -> str:
        """Build a docker login shell command for an image entry.

        Uses `echo <base64> | base64 -d | docker login <registry> -u <user> --password-stdin`.
        """
        registry = entry.registry
        if not registry:
            return f'echo "[image_pull] WARNING: No registry found in image {entry.image}, skipping login" >&2'
        return (
            f'echo "[image_pull] Logging in to {registry} as {entry.registry_username}..."; '
            f'echo "{entry.registry_password}" | base64 -d | '
            f'docker login "{registry}" --username "{entry.registry_username}" --password-stdin; '
            f"LOGIN_EXIT=$?; "
            f"if [ $LOGIN_EXIT -ne 0 ]; then "
            f'echo "[image_pull] Docker login to {registry} failed (exit=$LOGIN_EXIT)" >&2; '
            f"fi"
        )

    def _build_pull_command(self, entry: ImageEntry) -> str:
        """Build a docker pull shell command for an image entry.

        If credentials are configured, prepends a docker login before the pull.
        """
        parts = []
        if entry.needs_login:
            parts.append(self._build_login_command(entry))
        parts.append(
            f'echo "[image_pull] Pulling {entry.image}..."; '
            f'if docker pull "{entry.image}"; then '
            f'echo "[image_pull] Successfully pulled {entry.image}"; '
            f"else "
            f'echo "[image_pull] Failed to pull {entry.image}" >&2; '
            f"fi"
        )
        return "; ".join(parts)

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        """Pull all configured images on the target worker using nohup.

        Executes `docker pull` for each image sequentially in a background script.
        Each image pull runs independently — a failure does not block subsequent pulls.
        If an image has registry credentials, `docker login` is executed before its pull.

        Args:
            runtime: RemoteSandboxRuntime instance for the worker

        Returns:
            Result dict with pid and task status

        Raises:
            Exception: Re-raises any exception encountered during execution for external reporting
        """
        if not self.image_entries:
            logger.warning("No images configured for image pull task")
            return {"status": TaskStatusEnum.SUCCESS, "message": "no images configured"}

        worker_host = runtime._config.host
        collected_errors = []

        try:
            # Build shell script — each image is independent (login + pull)
            # Use double quotes inside bash -c '...' to avoid single-quote nesting issues
            script_body = "; ".join(self._build_pull_command(entry) for entry in self.image_entries)

            # Construct nohup command with log redirection
            log_redirect = '[ -n "$ROCK_LOGGING_PATH" ] && IMAGE_PULL_LOG="$ROCK_LOGGING_PATH/image_pull.log" || IMAGE_PULL_LOG="/dev/null"'
            command = (
                f"{log_redirect}; "
                f"nohup bash -c '{script_body}' "
                f'> "$IMAGE_PULL_LOG" 2>&1 & '
                f"echo {PID_PREFIX}${{!}}{PID_SUFFIX}"
            )

            result = await runtime.execute(Command(command=command, shell=True))

            if result.exit_code != 0:
                error_msg = f"Failed to start image pull task: exit_code={result.exit_code}, stderr={result.stderr}"
                collected_errors.append(error_msg)
                logger.error(f"[{self.type}] [{worker_host}] {error_msg}")
                raise RuntimeError(error_msg)

            pid = extract_nohup_pid(result.stdout)
            image_names = [entry.image for entry in self.image_entries]
            logger.info(
                f"[{self.type}] [{worker_host}] Image pull task [{pid}] started successfully, "
                f"pulling {len(self.image_entries)} images in background"
            )

            return {
                "pid": pid,
                "images": image_names,
                "status": TaskStatusEnum.RUNNING,
            }

        except Exception as e:
            collected_errors.append(str(e))
            logger.exception(f"[{self.type}] [{worker_host}] Exception in run_action: {e}")
            raise
