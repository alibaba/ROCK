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
from rock.sdk.model.server.config import ModelServiceConfig

# Configure logging
logger = init_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI, config: ModelServiceConfig):
    """Application lifespan context manager."""
    logger.info("LLM Service started")
    app.state.model_service_config = config
    yield
    logger.info("LLM Service shutting down")


def create_app(config: ModelServiceConfig) -> FastAPI:
    """Create FastAPI app with the given config."""
    app = FastAPI(
        title="LLM Service",
        description="Sandbox LLM Service for Agent and Roll communication",
        version="1.0.0",
        lifespan=lambda app: lifespan(app, config),
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

    return app


def main(
    model_servie_type: str,
    config: ModelServiceConfig,
):
    """Run the LLM Service."""
    # Create app and add router
    app = create_app(config)
    if model_servie_type == "local":
        asyncio.run(init_local_api())
        app.include_router(local_router, prefix="", tags=["local"])
    else:
        app.include_router(proxy_router, prefix="", tags=["proxy"])

    logger.info(f"Starting LLM Service on {config.host}:{config.port}, type: {model_servie_type}")
    uvicorn.run(app, host=config.host, port=config.port, log_level="info", reload=False)


def create_config_from_args(args) -> ModelServiceConfig:
    """Create ModelServiceConfig from command line arguments."""
    # Build config from file or defaults
    if args.config_file:
        try:
            config = ModelServiceConfig.from_file(args.config_file)
            logger.info(f"Model Service Config loaded from: {args.config_file}")
        except Exception as e:
            logger.error(f"Failed to load config from {args.config_file}: {e}")
            raise e
    else:
        config = ModelServiceConfig()
        logger.info("No config file specified. Using default config settings.")

    # Command line arguments override config
    if args.host:
        config.host = args.host
        logger.info(f"host set from command line: {args.host}")
    if args.port:
        config.port = args.port
        logger.info(f"port set from command line: {args.port}")
    if args.proxy_url:
        config.proxy_url = args.proxy_url
        logger.info(f"proxy_url set from command line: {args.proxy_url}")
    if args.retryable_status_codes:
        codes = [int(c.strip()) for c in args.retryable_status_codes.split(",")]
        config.retryable_status_codes = codes
        logger.info(f"retryable_status_codes set from command line: {codes}")
    if args.request_timeout:
        config.request_timeout = args.request_timeout
        logger.info(f"request_timeout set from command line: {args.request_timeout}s")

    return config


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
        "--host",
        type=str,
        default=None,
        help="Server host address. Overrides config file.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Server port. Overrides config file.",
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

    config = create_config_from_args(args)
    main(args.type, config)
