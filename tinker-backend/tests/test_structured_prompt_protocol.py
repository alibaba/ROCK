from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tinker_backend.adapters.rock_adapter import RockAdapter
from tinker_backend.protocol.enums import ActionState, RuntimeState
from tinker_backend.protocol.schemas import OpenAIChatPrompt, SampleRequest
from tinker_backend.server.sdk_routes import _wait_for_step_data, asample
from tinker_backend.services.action_dispatcher import create_action_with_future, post_action_result
from tinker_backend.services.environment_service import create_env, post_step
from tinker_backend.storage.models import Base, RuntimeActionRecord, RuntimeInstanceRecord


def _openai_request() -> dict[str, Any]:
    return {
        "model": "qwen-test",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "submit",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "temperature": 0.8,
    }


def _prompt_dict() -> dict[str, Any]:
    return OpenAIChatPrompt(request=_openai_request()).model_dump(mode="json")


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class StructuredPromptSchemaTest(unittest.TestCase):
    def test_sample_request_accepts_prompt_or_model_input_only(self) -> None:
        prompt = OpenAIChatPrompt(request=_openai_request())

        prompt_request = SampleRequest(prompt=prompt, sampling_params={"temperature": 0.8})
        self.assertEqual(prompt_request.prompt, prompt)
        self.assertIsNone(prompt_request.model_input)

        model_input = {"chunks": [{"type": "encoded_text", "tokens": [1, 2, 3]}]}
        token_request = SampleRequest(model_input=model_input)
        self.assertEqual(token_request.model_input, model_input)
        self.assertIsNone(token_request.prompt)

        with self.assertRaises(ValidationError):
            SampleRequest(prompt=prompt, model_input=model_input)
        with self.assertRaises(ValidationError):
            SampleRequest()
        with self.assertRaises(ValidationError):
            SampleRequest(model_input={})

    def test_prompt_schema_rejects_model_input_shape(self) -> None:
        with self.assertRaises(ValidationError):
            OpenAIChatPrompt(
                type="openai_chat_completion_request",
                version=1,
                request={"chunks": [{"type": "encoded_text", "tokens": [1]}]},
            )


class RockAdapterPromptEnvelopeTest(unittest.TestCase):
    def test_parse_llm_request_returns_openai_prompt_envelope(self) -> None:
        adapter = object.__new__(RockAdapter)

        prompt = adapter._parse_llm_request_to_prompt(json.dumps(_openai_request(), ensure_ascii=False))

        self.assertEqual(prompt["type"], "openai_chat_completion_request")
        self.assertEqual(prompt["version"], 1)
        self.assertEqual(prompt["request"], _openai_request())
        self.assertNotIn("chunks", prompt)

    def test_parse_llm_request_rejects_non_json_or_missing_messages(self) -> None:
        adapter = object.__new__(RockAdapter)

        for raw in ("plain text", json.dumps([{"role": "user"}]), json.dumps({"model": "qwen-test"})):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    adapter._parse_llm_request_to_prompt(raw)

    def test_parse_llm_request_accepts_cli_prefix_before_json(self) -> None:
        adapter = object.__new__(RockAdapter)

        prompt = adapter._parse_llm_request_to_prompt(
            "model-service output:\n" + json.dumps(_openai_request(), ensure_ascii=False)
        )

        self.assertEqual(prompt, _prompt_dict())

    def test_make_openai_response_preserves_text_tool_calls(self) -> None:
        tool_calls = [
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "submit", "arguments": "{}"},
            }
        ]
        response = RockAdapter._make_openai_response(
            {
                "sequences": [
                    {
                        "text": "done",
                        "tool_calls": tool_calls,
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )

        choice = response["choices"][0]
        self.assertEqual(choice["message"]["content"], "done")
        self.assertEqual(choice["message"]["tool_calls"], tool_calls)
        self.assertEqual(choice["finish_reason"], "tool_calls")


class BackendStructuredPromptRouteTest(unittest.TestCase):
    def test_get_step_returns_openai_prompt_envelope(self) -> None:
        async def run():
            engine = create_async_engine("sqlite+aiosqlite:///:memory:")
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
                async with sessionmaker() as session:
                    session.add(
                        RuntimeInstanceRecord(
                            runtime_id="rt_prompt",
                            runtime_type="roll",
                            config_type="yaml",
                            config_content="rock: {enabled: true}",
                            status=RuntimeState.READY.value,
                            ready=True,
                        )
                    )
                    env = await create_env(
                        session,
                        runtime_id="rt_prompt",
                        task_id="task_1",
                        dataset="dataset",
                        split="test",
                    )
                    await post_step(session, env.env_id, 0, prompt=_prompt_dict())
                    await session.commit()

                with patch("tinker_backend.server.sdk_routes.get_sessionmaker", return_value=sessionmaker):
                    response = await _wait_for_step_data("rt_prompt", env.env_id, 0, _ConnectedRequest())
                    return response.prompt.model_dump(mode="json") if response.prompt else None
            finally:
                await engine.dispose()

        self.assertEqual(asyncio.run(run()), _prompt_dict())

    def test_asample_stores_prompt_or_model_input_payload(self) -> None:
        async def run():
            engine = create_async_engine("sqlite+aiosqlite:///:memory:")
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
                async with sessionmaker() as session:
                    session.add(
                        RuntimeInstanceRecord(
                            runtime_id="rt_sample",
                            runtime_type="roll",
                            config_type="yaml",
                            config_content="rock: {enabled: true}",
                            status=RuntimeState.READY.value,
                            ready=True,
                        )
                    )
                    await session.commit()

                async with sessionmaker() as session:
                    await asample(
                        "rt_sample",
                        SampleRequest(prompt=OpenAIChatPrompt(request=_openai_request()), env_id="env_prompt"),
                        session,
                    )
                    await asample(
                        "rt_sample",
                        SampleRequest(model_input={"chunks": [{"type": "encoded_text", "tokens": [4, 5]}]}),
                        session,
                    )
                    await session.commit()

                async with sessionmaker() as session:
                    actions = (
                        await session.execute(select(RuntimeActionRecord).order_by(RuntimeActionRecord.action_id))
                    ).scalars().all()
                    return [action.payload for action in actions]
            finally:
                await engine.dispose()

        prompt_payload, model_input_payload = asyncio.run(run())
        self.assertEqual(prompt_payload["prompt"], _prompt_dict())
        self.assertIsNone(prompt_payload["model_input"])
        self.assertIsNone(model_input_payload["prompt"])
        self.assertEqual(model_input_payload["model_input"]["chunks"][0]["tokens"], [4, 5])

    def test_sample_completion_enqueues_deliver_sample_payload(self) -> None:
        async def run():
            engine = create_async_engine("sqlite+aiosqlite:///:memory:")
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
                async with sessionmaker() as session:
                    session.add(
                        RuntimeInstanceRecord(
                            runtime_id="rt_deliver",
                            runtime_type="roll",
                            config_type="yaml",
                            config_content="rock: {enabled: true}",
                            status=RuntimeState.READY.value,
                            ready=True,
                        )
                    )
                    env = await create_env(
                        session,
                        runtime_id="rt_deliver",
                        task_id="task_1",
                        dataset="dataset",
                        split="test",
                        env_metadata={"rock_managed": True},
                    )
                    action, _future = await create_action_with_future(
                        session,
                        runtime_id="rt_deliver",
                        action_type="sample",
                        payload={"prompt": _prompt_dict()},
                        env_id=env.env_id,
                        request_type="sample",
                    )
                    action.status = ActionState.CLAIMED.value
                    sample_response = {"sequences": [{"text": "hello", "finish_reason": "stop"}]}
                    await post_action_result(
                        session,
                        runtime_id="rt_deliver",
                        action_id=action.action_id,
                        status="completed",
                        result_data=sample_response,
                    )
                    await session.commit()

                async with sessionmaker() as session:
                    deliver_action = (
                        await session.execute(
                            select(RuntimeActionRecord).where(RuntimeActionRecord.action_type == "deliver_sample")
                        )
                    ).scalar_one()
                    return deliver_action.payload
            finally:
                await engine.dispose()

        payload = asyncio.run(run())
        self.assertEqual(payload["sample_response"], {"sequences": [{"text": "hello", "finish_reason": "stop"}]})
        self.assertIn("sample_action_id", payload)


if __name__ == "__main__":
    unittest.main()
