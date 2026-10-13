from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import tinker
from tinker_cookbook.completers import StopCondition, TokenCompleter, TokensWithLogprobs

logger = logging.getLogger(__name__)


@dataclass
class RuntimePromptCompleter(TokenCompleter):
    """Sample a human-readable chat prompt through an independent runtime."""

    sampling_client: Any
    max_tokens: int = 64
    temperature: float = 0.0

    async def __call__(
        self,
        prompt: tinker.OpenAIChatPrompt,
        stop: StopCondition,
        *,
        env_id: str | None = None,
    ) -> TokensWithLogprobs:
        if not isinstance(prompt, tinker.OpenAIChatPrompt):
            raise TypeError(
                f"RuntimePromptCompleter requires OpenAIChatPrompt, got {type(prompt)!r}"
            )

        started_at = time.monotonic()
        sampling_params = tinker.SamplingParams(
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            stop=stop or None,
        )
        sample_async = getattr(self.sampling_client, "sample_async", None)
        if callable(sample_async):
            sample = await sample_async(
                prompt=prompt,
                sampling_params=sampling_params,
                num_samples=1,
                env_id=env_id,
            )
        else:
            sample = await self.sampling_client.sample(
                prompt=prompt,
                sampling_params=sampling_params,
                num_samples=1,
                env_id=env_id,
            )

        if not sample.prompt_token_ids:
            raise RuntimeError("Runtime sample response did not include prompt_token_ids")
        sequence = sample.sequences[0]
        if sequence.logprobs is None:
            raise RuntimeError("Runtime sample response did not include output logprobs")
        logger.info(
            "RuntimePromptCompleter: sample ready env_id=%s prompt_tokens=%s output_tokens=%s elapsed=%.1fs",
            env_id,
            len(sample.prompt_token_ids),
            len(sequence.tokens),
            time.monotonic() - started_at,
        )
        return TokensWithLogprobs(
            tokens=list(sequence.tokens),
            maybe_logprobs=list(sequence.logprobs),
            stop_reason=sequence.stop_reason,
            prompt_model_input=tinker.ModelInput.from_ints(list(sample.prompt_token_ids)),
            text=sequence.text,
            raw_text=sequence.raw_text,
            tool_calls=sequence.tool_calls,
            finish_reason=sequence.finish_reason,
        )
