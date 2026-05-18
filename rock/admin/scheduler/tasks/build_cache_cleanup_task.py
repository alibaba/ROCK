"""Prune build/install caches (uv, pip) on each worker."""

import textwrap

from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.common.constants import SCHEDULER_LOG_NAME
from rock.logger import init_logger
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime

logger = init_logger(name="build_cache_cleanup", file_name=SCHEDULER_LOG_NAME)

# Map tool name -> shell snippet that prunes its cache. Each snippet uses an
# explicit if/then/else so the "not installed" message is only emitted when
# `command -v` actually fails — otherwise the legacy `a && b || c` form would
# also fire `c` when `b` failed (permission/disk error), misleading operators
# into thinking the tool was missing when in fact the prune itself errored.
_TOOL_COMMANDS: dict[str, str] = {
    "uv": textwrap.dedent(
        """\
        if command -v uv >/dev/null 2>&1; then
          uv cache prune 2>&1 || echo "uv: prune failed"
        else
          echo "uv: skipped (not installed)"
        fi"""
    ),
    "pip": textwrap.dedent(
        """\
        if command -v pip >/dev/null 2>&1; then
          pip cache purge 2>&1 || echo "pip: prune failed"
        else
          echo "pip: skipped (not installed)"
        fi"""
    ),
}


class BuildCacheCleanupTask(BaseTask):
    """Prune build/install caches for the configured tools.

    Each tool runs in its own self-skipping shell snippet, so a worker that
    lacks one of them won't fail the whole task. Default `tools` covers both
    common tools used in the standard worker image; restrict via yml if you
    want to skip one of them.
    """

    def __init__(
        self,
        interval_seconds: int = 86400,
        tools: list[str] | None = None,
    ):
        """
        Args:
            interval_seconds: Execution interval, default 24 hours.
            tools: Subset of {"uv", "pip"}; default both. Unknown names raise.
        """
        super().__init__(
            type="build_cache_cleanup",
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.IDEMPOTENT,
        )
        tools = tools if tools is not None else ["uv", "pip"]
        unknown = [t for t in tools if t not in _TOOL_COMMANDS]
        if unknown:
            raise ValueError(f"Unsupported build cache tool(s): {unknown}. Supported: {sorted(_TOOL_COMMANDS)}")
        self.tools = tools

    @classmethod
    def from_config(cls, task_config) -> "BuildCacheCleanupTask":
        return cls(
            interval_seconds=task_config.interval_seconds,
            tools=task_config.params.get("tools"),
        )

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        # Sequential `; ` (not `&& `): each tool's snippet already converts
        # "missing" to a soft echo, but `;` keeps any future tool that emits a
        # non-zero exit from short-circuiting the rest.
        snippets = [_TOOL_COMMANDS[t] for t in self.tools]
        command = "; ".join(snippets) if snippets else "echo 'no tools configured'"
        result = await runtime.execute(Command(command=command, shell=True, check=False))
        output = (result.stdout or "").strip()
        logger.info(
            f"[{self.type}] [{runtime._config.host}] cache prune done: "
            f"tools={self.tools}, exit={result.exit_code}, output_head={output[:200]}"
        )
        return {
            "status": TaskStatusEnum.SUCCESS,
            "tools": self.tools,
            "exit_code": result.exit_code,
            "output_head": output[:500],
        }
