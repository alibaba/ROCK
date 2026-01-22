"""LLM Service - FastAPI server for sandbox communication."""
import argparse
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from rock.logger import init_logger
from rock.sdk.model.server.api.local import init_local_api, local_router
from rock.sdk.model.server.api.proxy import proxy_router
from rock.sdk.model.server.config import SERVICE_HOST, SERVICE_PORT, ModelServiceConfig

# Configure logging
logger = init_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager."""
    logger.info("LLM Service started")
    config_path = getattr(app.state, "config_path", None)
    if config_path:
        try:
            app.state.model_service_config = ModelServiceConfig.from_file(config_path)
            logger.info(f"Model Service Config loaded from: {config_path}")
        except Exception as e:
            logger.error(f"Failed to load config from {config_path}: {e}")
            raise e
    else:
        app.state.model_service_config = ModelServiceConfig()
        logger.info("No config file specified. Using default config settings.")
    yield
    logger.info("LLM Service shutting down")


# Create FastAPI app
app = FastAPI(
    title="LLM Service",
    description="Sandbox LLM Service for Agent and Roll communication",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": {"message": str(exc), "type": "internal_error", "code": "internal_error"}},
    )


def main(model_servie_type: str, config_file: str | None):
    logger.info(f"Starting LLM Service on {SERVICE_HOST}:{SERVICE_PORT}, type: {model_servie_type}")
    app.state.config_path = config_file
    if model_servie_type == "local":
        asyncio.run(init_local_api())
        app.include_router(local_router, prefix="", tags=["local"])
    else:
        app.include_router(proxy_router, prefix="", tags=["proxy"])
    uvicorn.run(app, host=SERVICE_HOST, port=SERVICE_PORT, log_level="info", reload=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--type", type=str, choices=["local", "proxy"], default="local", help="Type of LLM service (local/proxy)"
    )
    parser.add_argument(
        "--config-file", type=str, default=None, help="Path to the configuration YAML file. If not set, default values will be used."
    )
    args = parser.parse_args()
    model_servie_type = args.type
    config_file = args.config_file

    main(model_servie_type, config_file)
