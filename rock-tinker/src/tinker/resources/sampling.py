# Derived from https://github.com/thinking-machines-lab/tinker (Apache-2.0).
# Modified in this Tinker fork; see rock-tinker/UPSTREAM.md and NOTICE.
from __future__ import annotations

import logging
from typing import cast

import httpx

from .._base_client import make_request_options
from .._compat import model_dump
from .._resource import AsyncAPIResource
from .._types import NOT_GIVEN, Body, Headers, NotGiven, Query
from ..types.get_step_request import GetStepRequest
from ..types.get_step_response import GetStepResponse
from ..types.init_task_env_request import InitTaskEnvRequest
from ..types.init_task_env_response import InitTaskEnvResponse
from ..types.sample_request import SampleRequest
from ..types.shared.untyped_api_future import UntypedAPIFuture

__all__ = ["AsyncSamplingResource"]


def _model_input_length(prompt: object) -> int:
    chunks = getattr(prompt, "chunks", None)
    if chunks is None and isinstance(prompt, dict):
        chunks = prompt.get("chunks")
    if not chunks:
        return 0
    total = 0
    for chunk in chunks:
        length = getattr(chunk, "length", None)
        if length is not None:
            total += int(length)
        elif isinstance(chunk, dict):
            total += len(chunk.get("tokens") or [])
    return total

logger = logging.getLogger("tinker.sampling")


class AsyncSamplingResource(AsyncAPIResource):
    async def asample(
        self,
        *,
        request: SampleRequest,
        # Use the following arguments if you need to pass additional parameters to the API that aren't available via kwargs.
        # The extra values given here take precedence over values defined on the client or passed to this method.
        extra_headers: Headers | None = None,
        extra_query: Query | None = None,
        extra_body: Body | None = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
        idempotency_key: str | None = None,
        max_retries: int | NotGiven = NOT_GIVEN,
    ) -> UntypedAPIFuture:
        """
        Generates samples from the model using the specified sampling parameters

        Args:
          request: The sample request containing prompt, sampling params, and options

          extra_headers: Send extra headers

          extra_query: Add additional query parameters to the request

          extra_body: Add additional JSON properties to the request

          timeout: Override the client-level default timeout for this request, in seconds

          idempotency_key: Specify a custom idempotency key for this request
        """
        logger.debug(
            "asample: session=%s, seq_id=%s, prompt_len=%d, num_samples=%d",
            request.sampling_session_id,
            request.seq_id,
            sum(c.length for c in request.prompt.chunks),
            request.num_samples,
        )
        options = make_request_options(
            extra_headers=extra_headers,
            extra_query=extra_query,
            extra_body=extra_body,
            timeout=timeout,
            idempotency_key=idempotency_key,
        )
        if max_retries is not NOT_GIVEN:
            options["max_retries"] = cast(int, max_retries)

        result = await self._post(
            "/api/v1/asample",
            body=model_dump(request, exclude_unset=False, exclude_none=True, mode="json"),
            options=options,
            cast_to=UntypedAPIFuture,
        )
        logger.debug(
            "asample response: request_id=%s, model_id=%s",
            result.request_id,
            result.model_id,
        )
        return result

    async def init_task_env(
        self,
        *,
        request: InitTaskEnvRequest,
        extra_headers: Headers | None = None,
        extra_query: Query | None = None,
        extra_body: Body | None = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
        idempotency_key: str | None = None,
        max_retries: int | NotGiven = NOT_GIVEN,
    ) -> InitTaskEnvResponse:
        """
        Initialize a task environment.

        Args:
          request: The init task env request containing task_id, dataset, split

          extra_headers: Send extra headers

          extra_query: Add additional query parameters to the request

          extra_body: Add additional JSON properties to the request

          timeout: Override the client-level default timeout for this request, in seconds

          idempotency_key: Specify a custom idempotency key for this request
        """
        logger.debug(
            "init_task_env: task_id=%s, dataset=%s, split=%s",
            request.task_id,
            request.dataset,
            request.split,
        )
        options = make_request_options(
            extra_headers=extra_headers,
            extra_query=extra_query,
            extra_body=extra_body,
            timeout=timeout,
            idempotency_key=idempotency_key,
        )
        if max_retries is not NOT_GIVEN:
            options["max_retries"] = cast(int, max_retries)

        result = await self._post(
            "/api/v1/init_task_env",
            body=model_dump(request, exclude_unset=False, exclude_none=True, mode="json"),
            options=options,
            cast_to=InitTaskEnvResponse,
        )
        logger.debug(
            "init_task_env response: env_id=%s",
            result.env_id,
        )
        return result

    async def get_step(
        self,
        *,
        request: GetStepRequest,
        extra_headers: Headers | None = None,
        extra_query: Query | None = None,
        extra_body: Body | None = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
        idempotency_key: str | None = None,
        max_retries: int | NotGiven = NOT_GIVEN,
    ) -> GetStepResponse:
        """
        Get a step for a task environment.

        Args:
          request: The get step request containing env_id and optional step_id

          extra_headers: Send extra headers

          extra_query: Add additional query parameters to the request

          extra_body: Add additional JSON properties to the request

          timeout: Override the client-level default timeout for this request, in seconds

          idempotency_key: Specify a custom idempotency key for this request
        """
        logger.debug(
            "get_step: env_id=%s, step_id=%s",
            request.env_id,
            request.step_id,
        )
        options = make_request_options(
            extra_headers=extra_headers,
            extra_query=extra_query,
            extra_body=extra_body,
            timeout=timeout,
            idempotency_key=idempotency_key,
        )
        if max_retries is not NOT_GIVEN:
            options["max_retries"] = cast(int, max_retries)

        result = await self._post(
            "/api/v1/get_step",
            body=model_dump(request, exclude_unset=False, exclude_none=True, mode="json"),
            options=options,
            cast_to=GetStepResponse,
        )
        logger.debug(
            "get_step response: step_id=%s, finish_reason=%s, reward=%s, prompt_len=%d",
            result.step_id,
            result.finish_reason,
            result.reward,
            _model_input_length(result.prompt),
        )
        return result
