from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch


class ServerJobShutdownRouteTest(unittest.TestCase):
    def test_server_job_stop_route_calls_graceful_runtime_shutdown(self) -> None:
        from tinker_backend.server import server_job_routes

        calls: list[tuple[object, float]] = []

        async def fake_stop_all_runtimes(session: object, *, force_after_seconds: float) -> dict:
            calls.append((session, force_after_seconds))
            return {"runtime_count": 2, "closed_runtime_ids": ["rt_a", "rt_b"], "errors": []}

        async def run() -> dict:
            session = object()
            with patch.object(server_job_routes, "stop_all_runtimes_for_server_job", fake_stop_all_runtimes):
                return await server_job_routes.stop_server_job(
                    server_job_routes.ServerJobStopRequest(force_after_seconds=12.5),
                    session=session,
                )

        response = asyncio.run(run())

        self.assertEqual(response["status"], "stopped")
        self.assertEqual(response["runtime_count"], 2)
        self.assertEqual(calls[0][1], 12.5)

    def test_app_registers_server_job_stop_route(self) -> None:
        from tinker_backend.server.app import create_app

        app = create_app()
        paths = set(app.openapi()["paths"])

        self.assertIn("/api/v1/server_job/stop", paths)


if __name__ == "__main__":
    unittest.main()
