from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch

import yaml
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tinker_backend.protocol.enums import RuntimeState
from tinker_backend.protocol.schemas import DatasetSpec, InitTaskEnvRequest, ListTasksRequest, TaskDescriptor
from tinker_backend.server.dataset_routes import list_tasks
from tinker_backend.server.sdk_routes import init_task_env
from tinker_backend.storage.models import Base, RuntimeActionRecord, RuntimeInstanceRecord, TaskEnvironmentRecord


class InitTaskEnvProtocolTest(unittest.TestCase):
    def test_request_uses_task_descriptor_field_names(self) -> None:
        request = InitTaskEnvRequest(
            task_id="sympy__sympy-19637",
            dataset="princeton-nlp/SWE-bench_Verified",
            split="test",
            metadata={"bench_name": "SWE-bench"},
        )

        self.assertEqual(
            request.model_dump(exclude_none=True),
            {
                "task_id": "sympy__sympy-19637",
                "dataset": "princeton-nlp/SWE-bench_Verified",
                "split": "test",
                "metadata": {"bench_name": "SWE-bench"},
            },
        )

    def test_legacy_instance_dataset_names_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            InitTaskEnvRequest(
                instance_id="sympy__sympy-19637",
                dataset_name="princeton-nlp/SWE-bench_Verified",
                dataset_type="test",
            )


class InitTaskEnvRouteTest(unittest.TestCase):
    def test_route_persists_task_descriptor_names_in_env_and_action_payload(self) -> None:
        async def run() -> tuple[TaskEnvironmentRecord, RuntimeActionRecord]:
            engine = create_async_engine("sqlite+aiosqlite:///:memory:")
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
                async with sessionmaker() as session:
                    session.add(
                        RuntimeInstanceRecord(
                            runtime_id="rt_task_descriptor",
                            runtime_type="roll",
                            config_type="yaml",
                            config_content=yaml.safe_dump({"rock": {"enabled": True}}),
                            status=RuntimeState.READY.value,
                            ready=True,
                        )
                    )
                    await session.commit()

                async with sessionmaker() as session:
                    await init_task_env(
                        "rt_task_descriptor",
                        InitTaskEnvRequest(
                            task_id="sympy__sympy-19637",
                            dataset="princeton-nlp/SWE-bench_Verified",
                            split="test",
                            metadata={"bench_name": "SWE-bench"},
                        ),
                        session,
                    )
                    await session.commit()

                async with sessionmaker() as session:
                    env = (await session.execute(select(TaskEnvironmentRecord))).scalar_one()
                    action = (await session.execute(select(RuntimeActionRecord))).scalar_one()
                    return env, action
            finally:
                await engine.dispose()

        env, action = asyncio.run(run())

        self.assertEqual(env.task_id, "sympy__sympy-19637")
        self.assertEqual(env.dataset, "princeton-nlp/SWE-bench_Verified")
        self.assertEqual(env.split, "test")
        self.assertEqual(env.env_metadata["bench_name"], "SWE-bench")
        self.assertTrue(env.env_metadata["rock_managed"])
        self.assertEqual(action.payload["task_id"], "sympy__sympy-19637")
        self.assertEqual(action.payload["dataset"], "princeton-nlp/SWE-bench_Verified")
        self.assertEqual(action.payload["split"], "test")
        self.assertNotIn("instance_id", action.payload)
        self.assertNotIn("dataset_name", action.payload)
        self.assertNotIn("dataset_type", action.payload)


class ListTasksRouteTest(unittest.TestCase):
    def test_list_tasks_returns_task_descriptors(self) -> None:
        async def run():
            with patch("tinker_backend.server.dataset_routes.list_task_descriptors") as list_descriptors:
                list_descriptors.return_value = [
                    TaskDescriptor(
                        task_id="sympy__sympy-19637",
                        dataset="princeton-nlp/SWE-bench_Verified",
                        split="test",
                        bench_name="SWE-bench",
                    )
                ]
                return await list_tasks(
                    ListTasksRequest(
                        datasets=[
                            DatasetSpec(
                                dataset="princeton-nlp/SWE-bench_Verified",
                                split="test",
                                bench_name="SWE-bench",
                                task_filter="^sympy__sympy-19637$",
                            )
                        ]
                    )
                )

        response = asyncio.run(run())

        self.assertEqual(response.total, 1)
        self.assertEqual(response.tasks[0].task_id, "sympy__sympy-19637")
        self.assertEqual(response.tasks[0].dataset, "princeton-nlp/SWE-bench_Verified")
        self.assertEqual(response.tasks[0].split, "test")
        self.assertEqual(response.tasks[0].bench_name, "SWE-bench")

    def test_invalid_catalog_request_returns_400(self) -> None:
        body = ListTasksRequest(
            datasets=[DatasetSpec(dataset="invalid", split="test", bench_name="SWE-bench")]
        )

        async def run() -> None:
            with patch(
                "tinker_backend.server.dataset_routes.list_task_descriptors",
                side_effect=ValueError("Dataset must use organization/name format"),
            ):
                await list_tasks(body)

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(run())
        self.assertEqual(raised.exception.status_code, 400)

    def test_unavailable_catalog_returns_503(self) -> None:
        body = ListTasksRequest(
            datasets=[DatasetSpec(dataset="organization/dataset", split="test", bench_name="SWE-bench")]
        )

        async def run() -> None:
            with patch(
                "tinker_backend.server.dataset_routes.list_task_descriptors",
                side_effect=FileNotFoundError("missing dataset split"),
            ):
                await list_tasks(body)

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(run())
        self.assertEqual(raised.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
