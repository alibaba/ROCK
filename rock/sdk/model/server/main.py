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
    proxy_url = getattr(app.state, "proxy_url", None)
    retryable_status_codes = getattr(app.state, "retryable_status_codes", None)
    request_timeout = getattr(app.state, "request_timeout", None)
    if config_path:
        try:
            app.state.model_service_config = ModelServiceConfig.from_file(config_path)
            # Command line arguments take precedence over config file
            if proxy_url:
                app.state.model_service_config.proxy_url = proxy_url
                logger.info(f"Override proxy_url from command line: {proxy_url}")
            logger.info(f"Model Service Config loaded from: {config_path}")
        except Exception as e:
            logger.error(f"Failed to load config from {config_path}: {e}")
            raise e
    else:
        app.state.model_service_config = ModelServiceConfig()
        if proxy_url:
            app.state.model_service_config.proxy_url = proxy_url
            logger.info(f"proxy_url set from command line: {proxy_url}")
        else:
            logger.info("No config file specified. Using default config settings.")

    # Override retryable_status_codes
    if retryable_status_codes:
        codes = [int(c.strip()) for c in retryable_status_codes.split(",")]
        app.state.model_service_config.retryable_status_codes = codes
        logger.info(f"Override retryable_status_codes: {codes}")

    # Override request_timeout
    if request_timeout:
        app.state.model_service_config.request_timeout = request_timeout
        logger.info(f"Override request_timeout: {request_timeout}s")

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


def main(
    model_servie_type: str,
    config_file: str | None,
    proxy_url: str | None,
    retryable_status_codes: str | None,
    request_timeout: int | None,
):
    logger.info(f"Starting LLM Service on {SERVICE_HOST}:{SERVICE_PORT}, type: {model_servie_type}")
    app.state.config_path = config_file
    app.state.proxy_url = proxy_url
    app.state.retryable_status_codes = retryable_status_codes
    app.state.request_timeout = request_timeout
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
        "--config-file",
        type=str,
        default=None,
        help="Path to the configuration YAML file. If not set, default values will be used.",
    )
    parser.add_argument(
        "--proxy-url",
        type=str,
        default=None,
        help="Direct proxy URL (e.g., https://your-endpoint.com/v1). Takes precedence over config file.",
    )
    parser.add_argument(
        "--retryable-status-codes",
        type=str,
        default=None,
        help="Retryable status codes, comma-separated (e.g., '429,500,502'). Overrides config file.",
    )
    parser.add_argument(
        "--request-timeout", type=int, default=None, help="Request timeout in seconds. Overrides config file."
    )
    args = parser.parse_args()
    model_servie_type = args.type
    config_file = args.config_file
    proxy_url = args.proxy_url
    retryable_status_codes = args.retryable_status_codes
    request_timeout = args.request_timeout

    main(model_servie_type, config_file, proxy_url, retryable_status_codes, request_timeout)
