"""Read-only Prometheus metrics endpoint for Grafana dashboards."""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.server.deps import get_db_session
from tinker_backend.services.observability_service import (
    collect_metrics_snapshot,
    render_prometheus_metrics,
)

router = APIRouter(tags=["observability"])


@router.get("/metrics")
async def prometheus_metrics(session: AsyncSession = Depends(get_db_session)) -> Response:
    """Expose aggregate backend state in Prometheus text format.

    The endpoint publishes only aggregate counters and latencies. It does not
    expose prompts, config content, payloads, secrets, or raw runtime results.
    """
    snapshot = await collect_metrics_snapshot(session)
    return Response(
        content=render_prometheus_metrics(snapshot),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
