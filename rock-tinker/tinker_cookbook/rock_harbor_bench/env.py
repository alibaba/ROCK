"""
RemoteSandboxEnv and RemoteSandboxEnvGroupBuilder for SWE-Bench tasks.

Uses SamplingClient.init_task_env() to initialize remote task environments
and get_current_step() to monitor progress during rollouts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import tinker
from typing_extensions import TypedDict

from tinker_cookbook.completers import StopCondition
from tinker_cookbook.rl.types import (
    Action,
    ActionExtra,
    Env,
    Observation,
    StepResult,
)

logger = logging.getLogger(__name__)


class TaskInfo(TypedDict, total=False):
    """Information about a SWE-Bench task."""
    task_name: str
    instance_id: str
    dataset_name: str
    dataset_type: str
    instanceid: str
    datasetname: str
    datasettype: str
    scaffold: str
    problem_statement: str | None


class RemoteSandboxEnv(Env):
    """Environment that uses a remote sandbox initialized via SamplingClient.

    Each environment instance corresponds to one task. The sandbox is created
    by calling sampling_client.init_task_env() during construction and
    monitored via get_current_step().

    This is a single-use environment: create it, run one episode, then discard.
    """

    def __init__(
        self,
        task_info: TaskInfo,
        sampling_client: tinker.SamplingClient,
        renderer,
        max_turns: int = 10,
        stop_sequences: list[str] | None = None,
    ):
        self.task_info = task_info
        self.sampling_client = sampling_client
        self.renderer = renderer
        self.max_turns = max_turns
        self._stop_sequences = stop_sequences or self.renderer.get_stop_sequences()
        self._env_id: str | None = None
        self._turn_count = 0
        self._messages: list[dict] = []
        self._done = False

    async def _init_sandbox(self) -> str:
        """Initialize the remote sandbox and return the env_id."""
        env_id = await asyncio.to_thread(
            self.sampling_client.init_task_env,
            instance_id=self.task_info["instance_id"],
            dataset_name=self.task_info.get("dataset_name", "rock-harbor-bench"),
            dataset_type=self.task_info.get("dataset_type", "grpo"),
        )
        logger.info("Initialized task env: env_id=%s, task=%s", env_id, self.task_info["task_name"])
        self._env_id = env_id
        return env_id

    def get_env_id(self) -> str | None:
        return self._env_id

    async def _get_step(self, step_id: int | None = None) -> tuple[int, Observation | None, float | None, tinker.types.FinishReason | None]:
        """Get a step from the remote environment.

        Args:
            step_id: Step number to retrieve (None means latest).

        Returns:
            Tuple of (step_id, prompt_ob, reward, finish_reason).
        """
        if self._env_id is None:
            raise RuntimeError("Sandbox not initialized. Call _init_sandbox first.")
        step_response = await asyncio.to_thread(
            self.sampling_client.get_step, env_id=self._env_id, step_id=step_id
        )
        prompt_data = step_response.prompt
        ob: Observation | None = None
        if prompt_data is not None:
            if isinstance(prompt_data, dict) and "chunks" in prompt_data:
                ob = tinker.ModelInput.model_validate(prompt_data)
            elif hasattr(prompt_data, "chunks"):
                ob = prompt_data
        return step_response.step_id, ob, step_response.reward, step_response.finish_reason

    async def get_observation(self, *, init: bool = False) -> tuple[Observation, StopCondition]:
        """Get observation for the current step.

        If init is True, initializes the sandbox first then gets step 0.
        Otherwise, gets the prompt for the current _turn_count.
        """
        if init:
            await self._init_sandbox()
            step_id, prompt_ob, reward, finish_reason = await self._get_step(step_id=0)
            if step_id != 0:
                raise RuntimeError(
                    f"Expected step_id 0 after init_task_env, got step_id {step_id}. "
                    f"Task env may already have been used: env_id={self._env_id}"
                )
            if prompt_ob is None:
                raise RuntimeError(
                    f"No prompt returned from get_step after init_task_env. "
                    f"env_id={self._env_id}"
                )
            logger.info(
                "Task env initialized: env_id=%s, step_id=%d, prompt_len=%d",
                self._env_id, step_id, prompt_ob.length,
            )
        else:
            step_id, prompt_ob, reward, finish_reason = await self._get_step(step_id=self._turn_count)

        if prompt_ob is None:
            # Fallback: build from conversation
            prompt_ob = self.renderer.build_generation_prompt(self._messages)

        return prompt_ob, self._stop_sequences

    async def step(self, action: Action, *, extra: ActionExtra | None = None) -> StepResult:
        """Advance the environment by one step given the agent's action.

        Decodes the agent's token action, executes it in the remote sandbox,
        and returns the result.
        """
        self._turn_count += 1

        # Decode the action tokens to text
        action_text = self.renderer.tokenizer.decode(action) if hasattr(self.renderer, "tokenizer") else str(action)

        # Add assistant message to conversation
        self._messages.append({"role": "assistant", "content": action_text})

        try:
            step_id, next_ob, reward, finish_reason = await self._get_step(step_id=self._turn_count)
            episode_done = finish_reason is not None
            # Episode finished: prompt is None but reward + finish_reason are set.
            # Surface the terminal record so do_single_rollout records the reward
            # on the last transition.
            if episode_done and next_ob is None:
                empty_ob = tinker.ModelInput.from_ints([])
                return StepResult(
                    reward=reward or 0.0,
                    episode_done=True,
                    next_observation=empty_ob,
                    next_stop_condition=[],
                    metrics={"turn": self._turn_count, "step_id": step_id},
                    logs={"finish_reason": str(finish_reason)},
                )
            if next_ob is not None:
                return StepResult(
                    reward=reward or 0.0,
                    episode_done=episode_done,
                    next_observation=next_ob,
                    next_stop_condition=[] if episode_done else self._stop_sequences,
                    metrics={"turn": self._turn_count, "step_id": step_id},
                    logs={"finish_reason": str(finish_reason) if finish_reason else "none"},
                )
        except Exception as e:
            logger.warning("Failed to get current step: %s", e)

        if self._turn_count >= self.max_turns:
            logger.info("Max turns (%d) reached, ending episode", self.max_turns)
            empty_ob = tinker.ModelInput.from_ints([])
            return StepResult(
                reward=0.0,
                episode_done=True,
                next_observation=empty_ob,
                next_stop_condition=[],
                metrics={"turn": self._turn_count},
                logs={"reason": "max_turns"},
            )

        next_model_input = self.renderer.build_generation_prompt(self._messages)
        return StepResult(
            reward=0.0,
            episode_done=False,
            next_observation=next_model_input,
            next_stop_condition=self._stop_sequences,
            metrics={"turn": self._turn_count},
        )

    def cleanup(self) -> None:
        """Clean up the remote sandbox if needed."""
        if self._env_id:
            logger.debug("Cleaning up env_id=%s", self._env_id)
            self._env_id = None


def load_harbor_tasks(data_path: str | Path) -> list[TaskInfo]:
    """Load tasks from a JSON or JSONL file.

    Each entry must have at least 'task_name' and 'instance_id'.
    'dataset_name', 'dataset_type', and 'problem_statement' are optional.
    """
    data_path = Path(data_path)
    if not data_path.exists():
        raise FileNotFoundError(f"Task data file not found: {data_path}")

    tasks: list[TaskInfo] = []

    # Map SWE-Env field names (no underscores) to TaskInfo field names (with underscores)
    KEY_MAP = {
        "instanceid": "instance_id",
        "datasetname": "dataset_name",
        "datasettype": "dataset_type",
    }

    def normalize(info: dict) -> TaskInfo:
        return TaskInfo(**{KEY_MAP.get(k, k): v for k, v in info.items()})

    if data_path.suffix == ".jsonl":
        with open(data_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    item = json.loads(line)
                    tasks.append(normalize(item["extra_info"]))
    else:
        with open(data_path) as f:
            data = json.load(f)
            if isinstance(data, list):
                tasks = [normalize(item["extra_info"]) for item in data]
            elif isinstance(data, dict):
                tasks = [normalize(data["extra_info"])]
            else:
                raise ValueError(f"Unsupported JSON format in {data_path}")

    return tasks
