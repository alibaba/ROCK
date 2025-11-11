import argparse
import logging
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from rock import env_vars
from rock.admin.core.ray_service import RayService
from rock.admin.entrypoints.sandbox_api import sandbox_router, set_sandbox_manager
from rock.admin.entrypoints.sandbox_proxy_api import sandbox_proxy_router, set_sandbox_proxy_service
from rock.admin.entrypoints.warmup_api import set_warmup_service, warmup_router
from rock.admin.gem.api import gem_router, set_env_service
from rock.config import RockConfig
from rock.logger import init_logger
from rock.sandbox.gem_manager import GemManager
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sandbox.service.warmup_service import WarmupService
from rock.utils import sandbox_id_ctx_var
from rock.utils.providers import RedisProvider

parser = argparse.ArgumentParser()
parser.add_argument("--env", type=str, default="local")
parser.add_argument("--role", type=str, default="admin", choices=["admin", "proxy"])
parser.add_argument("--port", type=int, default=8080)

args = parser.parse_args()

logger = init_logger("admin")
logging.getLogger("urllib3").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_file_path = (
        Path(__file__).resolve().parents[2] / env_vars.ROCK_CONFIG_DIR_NAME / f"rock-{args.env}.yml"
        if not env_vars.ROCK_CONFIG
        else env_vars.ROCK_CONFIG
    )
    rock_config = RockConfig.from_env(config_file_path)
    env_vars.ROCK_ADMIN_ENV = args.env
    env_vars.ROCK_ADMIN_ROLE = args.role

    # init redis provider
    if args.env == "local":
        redis_provider = None
    else:
        redis_provider = RedisProvider(
            host=rock_config.redis.host,
            port=rock_config.redis.port,
            password=rock_config.redis.password,
        )
        await redis_provider.init_pool()

    # init sandbox service
    if args.role == "admin":
        # init service
        if rock_config.runtime.enable_auto_clear:
            sandbox_manager = GemManager(
                rock_config,
                redis_provider=redis_provider,
                ray_namespace=rock_config.ray.namespace,
                enable_runtime_auto_clear=True,
            )
        else:
            sandbox_manager = GemManager(
                rock_config,
                redis_provider=redis_provider,
                ray_namespace=rock_config.ray.namespace,
                enable_runtime_auto_clear=False,
            )
        set_sandbox_manager(sandbox_manager)
        warmup_service = WarmupService(rock_config.warmup)
        await warmup_service.init()
        set_warmup_service(warmup_service)
        set_env_service(sandbox_manager)

        RayService(rock_config.ray).init()
    else:
        sandbox_manager = SandboxProxyService(rock_config=rock_config, redis_provider=redis_provider)
        set_sandbox_proxy_service(sandbox_manager)

    logger.info("rock-admin start")

    yield

    if redis_provider:
        await redis_provider.close_pool()

    logger.info("rock-admin exit")


app = FastAPI(lifespan=lifespan)

# --- CORS configuration start ---
# Allowed origins list
origins = [
    "*",  # Your frontend origin
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # Set allowed origins
    allow_credentials=True,  # Whether to support cookie cross-origin
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)
# --- CORS configuration end ---


@app.exception_handler(Exception)
async def base_exception_handler(request: Request, exc: Exception):
    exc_content = {"detail": str(exc), "traceback": traceback.format_exc().split("\n")}
    logger.error(f"[app error] request:[{request}], exc:[{exc_content}]")
    return JSONResponse(status_code=500, content=exc_content)


@app.get("/")
async def root():
    return {"message": "hello, ROCK!"}


@app.middleware("http")
async def log_requests_and_responses(request: Request, call_next):
    req_logger = init_logger("accessLog")

    body_json = dict(request.query_params)
    if request.headers.get("content-type", "").lower().startswith("application/json"):
        try:
            body_json = await request.json()
        except Exception as e:
            req_logger.error(f"Could not decode request body:{body_json}, error:{e}")
            body_json = {}

        # Get SANDBOX_ID from JSON field
        sandbox_id = body_json.get("sandbox_id")
        if sandbox_id is not None:
            sandbox_id_ctx_var.set(sandbox_id)

    req_logger.info(
        f"request method={request.method} url={request.url} "
        f"sandbox_id={sandbox_id_ctx_var.get()} "
        f"trace_id={request.headers.get('EagleEye-TraceId')} "
        f"headers={dict(request.headers)} request={body_json}"
    )

    # Process request and log response
    start_time = time.perf_counter()
    response = await call_next(request)
    process_time = f"{(time.perf_counter() - start_time) * 1000:.2f}ms"

    req_logger.info(
        f"response status_code={response.status_code} process_time={process_time} sandbox_id={sandbox_id_ctx_var.get()}"
    )

    return response


def main():
    # config router
    if args.role == "admin":
        app.include_router(sandbox_router, prefix="/apis/envs/sandbox/v1", tags=["sandbox"])
    else:
        app.include_router(sandbox_proxy_router, prefix="/apis/envs/sandbox/v1", tags=["sandbox"])
    app.include_router(warmup_router, prefix="/apis/envs/sandbox/v1", tags=["warmup"])
    app.include_router(gem_router, prefix="/apis/v1/envs/gem", tags=["gem"])

    uvicorn.run(app, host="0.0.0.0", port=args.port, ws_ping_interval=None, ws_ping_timeout=None, timeout_keep_alive=30)


if __name__ == "__main__":
    main()
