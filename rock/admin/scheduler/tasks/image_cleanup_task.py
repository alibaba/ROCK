# rock/admin/scheduler/tasks/image_cleanup_task.py
from rock import env_vars
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import PID_PREFIX, PID_SUFFIX, SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.utils.system import extract_nohup_pid

logger = init_logger(name="image_clean", file_name=SCHEDULER_LOG_NAME)


class ImageCleanupTask(BaseTask):
    """Docker image cleanup: docuum LRU + dangling/BuildKit prune.

    Two complementary cleanups in one task:
    - docuum: long-running daemon, LRU eviction of whole image:tag entries.
      Honors ``image_whitelist``.
    - ``docker image prune --filter dangling=true`` + ``docker builder prune
      --keep-storage <X>``: one-shot synchronous sweep of dangling layers
      (``<none>:<none>``) and BuildKit cache, which docuum never touches.
      Whitelist is intentionally NOT plumbed through here — dangling layers
      and BuildKit cache entries have no image:tag, so a whitelist would be
      misleading on both subcommands.

    Set ``keep_build_storage`` to a falsy value to disable the prune step.
    """

    def __init__(
        self,
        interval_seconds: int = 3600,
        disk_threshold: str = "1T",
        image_whitelist: list[str] | None = None,
        keep_build_storage: str | None = "20GB",
    ):
        """
        Args:
            interval_seconds: Execution interval, default 1 hour
            disk_threshold: Disk threshold to trigger docuum cleanup, default 1T
            image_whitelist: Regex patterns of images to keep (matched against
                repository:tag). Applies to docuum only.
            keep_build_storage: Lower bound for BuildKit cache retention,
                passed to ``docker builder prune --keep-storage``. Default
                "20GB". Set to None / empty to skip the prune step.
        """
        super().__init__(
            type="image_cleanup",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.NON_IDEMPOTENT,
            process_name="docuum",
        )
        self.disk_threshold = disk_threshold
        self.image_whitelist = image_whitelist or []
        self.keep_build_storage = keep_build_storage

    @classmethod
    def from_config(cls, task_config) -> "ImageCleanupTask":
        """Create task instance from config."""
        return cls(
            interval_seconds=task_config.interval_seconds,
            disk_threshold=task_config.params.get("disk_threshold", "1T"),
            image_whitelist=task_config.params.get("image_whitelist", []),
            keep_build_storage=task_config.params.get("keep_build_storage", "20GB"),
        )

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        """NON-idempotent path: launch docuum daemon (gated by should_run via base run_on_worker)."""
        return await self._launch_docuum(runtime)

    async def _launch_docuum(self, runtime: RemoteSandboxRuntime) -> dict:
        """docuum: LRU image eviction (long-running, nohup &). NON-idempotent."""
        check_and_install_cmd = (
            f"command -v docuum > /dev/null 2>&1 || curl {env_vars.ROCK_DOCUUM_INSTALL_URL} -LSfs | sh"
        )
        await runtime.execute(Command(command=check_and_install_cmd, shell=True, sandbox_id="scheduler-task"))

        log_redirect = (
            '[ -n "$ROCK_LOGGING_PATH" ] && DOCUUM_LOG="$ROCK_LOGGING_PATH/docuum.log" || DOCUUM_LOG="/dev/null"'
        )
        keep_args = " ".join(f"--keep '{pattern}'" for pattern in self.image_whitelist)
        docuum_cmd = f"docuum --threshold {self.disk_threshold}"
        if keep_args:
            docuum_cmd = f"{docuum_cmd} {keep_args}"
        command = f'{log_redirect}; nohup {docuum_cmd} > "$DOCUUM_LOG" 2>&1 & echo {PID_PREFIX}${{!}}{PID_SUFFIX}'
        result = await runtime.execute(Command(command=command, shell=True, sandbox_id="scheduler-task"))

        pid = extract_nohup_pid(result.stdout)
        logger.info(f"docuum launched with PID [{pid}] on worker[{runtime._config.host}]")
        return {
            "pid": pid,
            "disk_threshold": self.disk_threshold,
            "image_whitelist": self.image_whitelist,
            "status": TaskStatusEnum.RUNNING,
        }

    async def _run_prune(self, runtime: RemoteSandboxRuntime) -> dict:
        """Dangling/BuildKit prune (sync, fail-soft). IDEMPOTENT — runs every cycle."""
        if not self.keep_build_storage:
            return {"prune_exit_code": None, "prune_output_head": ""}
        prune_steps = [
            "docker image prune -f --filter dangling=true",
            f"docker builder prune -f --keep-storage {self.keep_build_storage}",
        ]
        prune_cmd = "; ".join(f"({s}) 2>&1 || true" for s in prune_steps)
        prune_result = await runtime.execute(
            Command(command=prune_cmd, shell=True, check=False, sandbox_id="scheduler-task")
        )
        prune_output = (prune_result.stdout or "").strip()[:1000]
        prune_exit = prune_result.exit_code
        logger.info(
            f"docker prune done on worker[{runtime._config.host}]: "
            f"keep_build_storage={self.keep_build_storage}, exit={prune_exit}, "
            f"output_head={prune_output[:300]}"
        )
        return {
            "keep_build_storage": self.keep_build_storage,
            "prune_exit_code": prune_exit,
            "prune_output_head": prune_output,
        }

    async def run_on_worker(self, ip):
        """Override base: prune unconditionally (idempotent), then gate docuum on should_run.

        The base run_on_worker skips entire task when should_run returns False, which
        correctly prevents docuum re-launch but wrongly blocks the idempotent prune
        step (dangling layers / BuildKit cache pile up forever once docuum is alive).
        """
        runtime = self._get_runtime(ip)
        # prune always runs — idempotent, fail-soft
        prune_result = {}
        try:
            prune_result = await self._run_prune(runtime)
        except Exception as e:
            logger.warning(f"[{self.type}] prune failed on worker[{ip}]: {e}")
        # docuum gated by should_run — non-idempotent
        if not await self.should_run(runtime):
            logger.info(f"[{self.type}] docuum already running on worker[{ip}], skip launch")
            return {
                "status": TaskStatusEnum.SUCCESS,
                "action": "prune_only",
                **prune_result,
            }
        logger.info(f"[{self.type}] launch docuum on worker[{ip}]")
        result = await self.single_run(runtime, ip)
        return {**result, **prune_result}
