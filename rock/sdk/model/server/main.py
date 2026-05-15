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
from rock.sdk.model.server.config import TRAJ_FILE, ModelServiceConfig

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


def _configure_proxy_integrations(app: FastAPI, config: ModelServiceConfig) -> None:
    """Attach the appropriate backend to ``app.state.backend``.

    - Replay mode (``replay_file`` set): ``ReplayBackend`` wrapping a
      ``SequentialCursor``; no recorder — replaying back into the source file
      would corrupt it.
    - Forward mode (default): ``ForwardBackend`` with a ``TrajectoryRecorder``
      writing to ``recording_file`` (or ``TRAJ_FILE`` if unset).
    """
    from rock.sdk.model.server.api.proxy import ForwardBackend, ReplayBackend

    if config.replay_file:
        from rock.sdk.model.server.traj import SequentialCursor

        cursor = SequentialCursor.load(config.replay_file)
        app.state.backend = ReplayBackend(cursor)
        logger.info(f"replay backend attached, replay_file={config.replay_file}")
        return

    from rock.sdk.model.server.traj import TrajectoryRecorder

    recording_path = config.recording_file or TRAJ_FILE
    recorder = TrajectoryRecorder(traj_file=recording_path)
    app.state.backend = ForwardBackend(config, recorder=recorder)
    logger.info(f"forward backend attached, recording_file={recording_path}")


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
        _configure_proxy_integrations(app, config)
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
    if args.proxy_base_url:
        config.proxy_base_url = args.proxy_base_url
        logger.info(f"proxy_base_url set from command line: {args.proxy_base_url}")
    if args.retryable_status_codes:
        codes = [int(c.strip()) for c in args.retryable_status_codes.split(",")]
        config.retryable_status_codes = codes
        logger.info(f"retryable_status_codes set from command line: {codes}")
    if args.request_timeout:
        config.request_timeout = args.request_timeout
        logger.info(f"request_timeout set from command line: {args.request_timeout}s")
    if args.recording_file:
        config.recording_file = args.recording_file
        logger.info(f"recording_file set from command line: {args.recording_file}")
    if args.replay_file:
        config.replay_file = args.replay_file
        logger.info(f"replay mode enabled via --replay-file: {args.replay_file}")

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
        "--proxy-base-url",
        type=str,
        default=None,
        help="Direct proxy base URL (e.g., https://your-endpoint.com/v1). Takes precedence over config file.",
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
    parser.add_argument(
        "--recording-file",
        type=str,
        default=None,
        help="Forward mode: where to write the trajectory JSONL. Defaults to TRAJ_FILE.",
    )
    parser.add_argument(
        "--replay-file",
        type=str,
        default=None,
        help="Replay mode: path to a recorded .jsonl traj file. Disables real LLM upstreams.",
    )
    args = parser.parse_args()

    config = create_config_from_args(args)
    main(args.type, config)
