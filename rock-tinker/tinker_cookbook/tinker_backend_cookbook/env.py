"""Remote task environment backed by tinker-backend and ROCK."""

from __future__ import annotations

import logging
import time
from typing import Any

import tinker
from tinker_cookbook.completers import StopCondition
from tinker_cookbook.rl.types import (
    Action,
    ActionExtra,
    Env,
    EnvironmentObservation,
    StepResult,
)

logger = logging.getLogger(__name__)


class RemoteSandboxEnv(Env):
    def __init__(
        self,
        task: tinker.TaskDescriptor,
        runtime: Any,
        max_turns: int = 10,
        stop_sequences: list[str] | list[int] | None = None,
    ):
        self.task = task
        self.runtime = runtime
        self.max_turns = max_turns
        self._stop_sequences = stop_sequences or []
        self._env_id: str | None = None
        self._turn_count = 0

    async def _init_sandbox(self) -> str:
        started_at = time.monotonic()
        logger.info(
            "RemoteSandboxEnv: init_task_env start task_id=%s dataset=%s/%s bench=%s",
            self.task.task_id,
            self.task.dataset,
            self.task.split,
            self.task.bench_name,
        )
        env_id = await self.runtime.init_task_env(self.task)
        logger.info(
            "RemoteSandboxEnv: init_task_env ready env_id=%s task_id=%s elapsed=%.1fs",
            env_id,
            self.task.task_id,
            time.monotonic() - started_at,
        )
        self._env_id = env_id
        return env_id

    def get_env_id(self) -> str | None:
        return self._env_id

    async def _get_step(
        self, step_id: int | None = None
    ) -> tuple[int, tinker.OpenAIChatPrompt | None, float | None, tinker.types.FinishReason | None]:
        if self._env_id is None:
            raise RuntimeError("Sandbox not initialized. Call _init_sandbox first.")
        started_at = time.monotonic()
        logger.info("RemoteSandboxEnv: get_step start env_id=%s step_id=%s", self._env_id, step_id)
        step_response = await self.runtime.get_step(env_id=self._env_id, step_id=step_id)
        prompt = step_response.prompt
        if prompt is not None and not isinstance(prompt, tinker.OpenAIChatPrompt):
            prompt = tinker.OpenAIChatPrompt.model_validate(prompt)
        logger.info(
            "RemoteSandboxEnv: get_step ready env_id=%s step_id=%s has_prompt=%s reward=%s finish_reason=%s elapsed=%.1fs",
            self._env_id,
            step_response.step_id,
            prompt is not None,
            step_response.reward,
            step_response.finish_reason,
            time.monotonic() - started_at,
        )
        return step_response.step_id, prompt, step_response.reward, step_response.finish_reason

    async def get_observation(
        self, *, init: bool = False
    ) -> tuple[EnvironmentObservation, StopCondition]:
        if init:
            await self._init_sandbox()
            step_id, prompt, _reward, _finish_reason = await self._get_step(step_id=0)
            if step_id != 0:
                raise RuntimeError(f"Expected step_id 0 after init_task_env, got {step_id}")
        else:
            _step_id, prompt, _reward, _finish_reason = await self._get_step(
                step_id=self._turn_count
            )

        if prompt is None:
            raise RuntimeError(
                f"No structured prompt returned from get_step for env_id={self._env_id}"
            )
        return prompt, self._stop_sequences

    async def step(self, action: Action, *, extra: ActionExtra | None = None) -> StepResult:
        del action, extra
        self._turn_count += 1
        logger.info(
            "RemoteSandboxEnv: step start env_id=%s turn=%s",
            self._env_id,
            self._turn_count,
        )
        try:
            step_id, next_prompt, reward, finish_reason = await self._get_step(
                step_id=self._turn_count
            )
        except Exception as exc:
            logger.exception(
                "RemoteSandboxEnv: get_step failed env_id=%s turn=%s",
                self._env_id,
                self._turn_count,
            )
            raise RuntimeError(
                f"Failed to get step for env_id={self._env_id} turn={self._turn_count}: {exc}"
            ) from exc

        episode_done = finish_reason is not None
        if episode_done:
            return StepResult(
                reward=reward or 0.0,
                episode_done=True,
                next_observation=tinker.ModelInput.empty(),
                next_stop_condition=[],
                metrics={"turn": self._turn_count, "step_id": step_id},
                logs={"finish_reason": str(finish_reason)},
            )
        if next_prompt is None:
            raise RuntimeError(
                f"Non-terminal step {step_id} has no structured prompt for env_id={self._env_id}"
            )
        return StepResult(
            reward=reward or 0.0,
            episode_done=False,
            next_observation=next_prompt,
            next_stop_condition=self._stop_sequences,
            metrics={"turn": self._turn_count, "step_id": step_id},
            logs={"finish_reason": "none"},
        )

    def cleanup(self) -> None:
        if self._env_id:
            logger.debug("Cleaning up env_id=%s", self._env_id)
            self._env_id = None
