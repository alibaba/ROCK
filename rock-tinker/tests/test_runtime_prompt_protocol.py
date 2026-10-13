from __future__ import annotations

import pytest
from pydantic import ValidationError

import tinker
from tinker_cookbook.completers import TokensWithLogprobs
from tinker_cookbook.rl.rollouts import do_single_rollout
from tinker_cookbook.rl.types import Action, ActionExtra, Env, StepResult
from tinker_cookbook.tinker_backend_cookbook.completer import RuntimePromptCompleter
from tinker_cookbook.tinker_backend_cookbook.trajectory_format import (
    format_structured_trajectory,
)


def _chat_prompt() -> tinker.OpenAIChatPrompt:
    return tinker.OpenAIChatPrompt(
        request={
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [],
        }
    )


def test_runtime_sample_request_keeps_chat_and_token_inputs_disjoint() -> None:
    prompt = _chat_prompt()
    model_input = tinker.ModelInput.from_ints([1, 2])
    params = tinker.SamplingParams(max_tokens=2)

    assert tinker.RuntimeSampleRequest(prompt=prompt, sampling_params=params).prompt == prompt
    assert (
        tinker.RuntimeSampleRequest(model_input=model_input, sampling_params=params).model_input
        == model_input
    )
    with pytest.raises(ValidationError):
        tinker.RuntimeSampleRequest(sampling_params=params)
    with pytest.raises(ValidationError):
        tinker.RuntimeSampleRequest(
            prompt=prompt,
            model_input=model_input,
            sampling_params=params,
        )

    # The original non-runtime API remains token-only.
    assert (
        tinker.SampleRequest(
            prompt=model_input,
            sampling_params=params,
        ).prompt
        == model_input
    )


@pytest.mark.asyncio
async def test_structured_rollout_records_runtime_prompt_tokens() -> None:
    prompt = _chat_prompt()

    class SamplingClientStub:
        async def sample_async(self, **kwargs):
            assert kwargs["prompt"] == prompt
            return tinker.SampleResponse(
                prompt_token_ids=[101, 102, 103],
                sequences=[
                    tinker.SampledSequence(
                        tokens=[201, 202],
                        logprobs=[-0.1, -0.2],
                        stop_reason="stop",
                        text="I will inspect the repository.",
                        raw_text='<tool_call>{"name":"shell"}</tool_call>',
                        tool_calls=[
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "shell",
                                    "arguments": '{"command":"pwd"}',
                                },
                            }
                        ],
                        finish_reason="tool_calls",
                    )
                ],
            )

    class EnvStub(Env):
        async def get_observation(self, *, init: bool = False):
            assert init is True
            return prompt, []

        async def step(self, action: Action, *, extra: ActionExtra | None = None) -> StepResult:
            assert action == [201, 202]
            return StepResult(
                reward=1.0,
                episode_done=True,
                next_observation=tinker.ModelInput.empty(),
                next_stop_condition=[],
            )

    trajectory = await do_single_rollout(
        RuntimePromptCompleter(
            sampling_client=SamplingClientStub(),
            max_tokens=2,
            temperature=0.8,
        ),
        EnvStub(),
    )

    assert len(trajectory.transitions) == 1
    assert trajectory.transitions[0].ob.to_ints() == [101, 102, 103]
    assert trajectory.transitions[0].ac.tokens == [201, 202]
    assert trajectory.transitions[0].raw_observation == prompt
    assert trajectory.transitions[0].ac.text == "I will inspect the repository."
    assert trajectory.transitions[0].ac.finish_reason == "tool_calls"
    assert trajectory.transitions[0].ac.tool_calls == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "shell",
                "arguments": '{"command":"pwd"}',
            },
        }
    ]

    rendered = format_structured_trajectory(trajectory)
    assert '"content": "hello"' in rendered
    assert "I will inspect the repository." in rendered
    assert '"name": "shell"' in rendered
    assert "prompt_tokens=3" in rendered
    assert "output_tokens=2" in rendered


@pytest.mark.asyncio
async def test_structured_rollout_requires_exact_runtime_prompt_tokens() -> None:
    prompt = _chat_prompt()

    class MissingPromptTokensCompleter:
        async def __call__(self, model_input, stop, *, env_id=None):
            return TokensWithLogprobs(tokens=[201], maybe_logprobs=[-0.1])

    class EnvStub(Env):
        async def get_observation(self, *, init: bool = False):
            return prompt, []

        async def step(self, action: Action, *, extra: ActionExtra | None = None) -> StepResult:
            return StepResult(
                reward=0.0,
                episode_done=True,
                next_observation=tinker.ModelInput.empty(),
                next_stop_condition=[],
            )

    with pytest.raises(
        RuntimeError,
        match="TokenCompleter did not provide prompt_model_input.*OpenAIChatPrompt",
    ):
        await do_single_rollout(MissingPromptTokensCompleter(), EnvStub())
