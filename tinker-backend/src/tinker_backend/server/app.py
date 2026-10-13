"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from tinker_backend.config import get_settings
from tinker_backend.server.zstd_middleware import ZstdMiddleware
from tinker_backend.storage.engine import dispose_engine, get_engine
from tinker_backend.storage.migrations import ensure_compatible_schema
from tinker_backend.storage.models import Base


class _SuppressClaimAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return "/api/v1/runtimes/" not in message or "/actions/claim" not in message


def _install_access_log_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if any(isinstance(item, _SuppressClaimAccessLogFilter) for item in access_logger.filters):
        return
    access_logger.addFilter(_SuppressClaimAccessLogFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_compatible_schema(conn)
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    _install_access_log_filter()
    application = FastAPI(
        title="Tinker Backend",
        version="0.1.0",
        description="Local-first control plane for SDK-driven training runtimes.",
        lifespan=lifespan,
    )
    application.add_middleware(ZstdMiddleware)

    from tinker_backend.server.health_routes import router as health_router
    from tinker_backend.server.session_routes import router as session_router
    from tinker_backend.server.runtime_routes import router as runtime_router
    from tinker_backend.server.sdk_routes import router as sdk_router
    from tinker_backend.server.adapter_routes import router as adapter_router
    from tinker_backend.server.observability_routes import router as observability_router
    from tinker_backend.server.dataset_routes import router as dataset_router
    from tinker_backend.server.server_job_routes import router as server_job_router

    application.include_router(health_router)
    application.include_router(session_router)
    application.include_router(runtime_router)
    application.include_router(sdk_router)
    application.include_router(adapter_router)
    application.include_router(observability_router)
    application.include_router(dataset_router)
    application.include_router(server_job_router)

    return application


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "tinker_backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.app_env == "dev",
    )
