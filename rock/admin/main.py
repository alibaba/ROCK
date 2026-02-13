import argparse
import json
import logging
import time
import traceback
import uuid
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
from rock.admin.scheduler.scheduler import SchedulerProcess
from rock.config import RockConfig
from rock.logger import init_logger
from rock.sandbox.gem_manager import GemManager
from rock.sandbox.operator.factory import OperatorContext, OperatorFactory
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sandbox.service.warmup_service import WarmupService
from rock.utils import EAGLE_EYE_TRACE_ID, sandbox_id_ctx_var, trace_id_ctx_var
from rock.utils.providers import RedisProvider
from rock.utils.system import is_primary_pod

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
    if args.env in ["local", "test"]:
        from fakeredis import aioredis

        redis_provider = RedisProvider(host=None, port=None, password="")
        redis_provider.client = aioredis.FakeRedis(decode_responses=True)
    else:
        redis_provider = RedisProvider(
            host=rock_config.redis.host,
            port=rock_config.redis.port,
            password=rock_config.redis.password,
        )
        await redis_provider.init_pool()

    # init scheduler process
    scheduler_process = None

    # init sandbox service
    if args.role == "admin":
        # init ray service
        ray_service = RayService(rock_config.ray)
        ray_service.init()

        # create operator using factory with context pattern
        operator_context = OperatorContext(
            runtime_config=rock_config.runtime,
            ray_service=ray_service,
            nacos_provider=rock_config.nacos_provider,
        )
        operator = OperatorFactory.create_operator(operator_context)

        # init service
        if rock_config.runtime.enable_auto_clear:
            sandbox_manager = GemManager(
                rock_config,
                redis_provider=redis_provider,
                ray_namespace=rock_config.ray.namespace,
                ray_service=ray_service,
                enable_runtime_auto_clear=True,
                operator=operator,
            )
        else:
            sandbox_manager = GemManager(
                rock_config,
                redis_provider=redis_provider,
                ray_namespace=rock_config.ray.namespace,
                ray_service=ray_service,
                enable_runtime_auto_clear=False,
                operator=operator,
            )
        set_sandbox_manager(sandbox_manager)
        warmup_service = WarmupService(rock_config.warmup)
        await warmup_service.init()
        set_warmup_service(warmup_service)
        set_env_service(sandbox_manager)

        if rock_config.scheduler.enabled and is_primary_pod():
            scheduler_process = SchedulerProcess(
                scheduler_config=rock_config.scheduler,
                ray_address=rock_config.ray.address,
                ray_namespace=rock_config.ray.namespace,
            )
            scheduler_process.start()
            logger.info("Scheduler process started on primary pod")
        elif rock_config.scheduler.enabled:
            logger.info("Scheduler process skipped on non-primary pod")

    else:
        sandbox_manager = SandboxProxyService(rock_config=rock_config, redis_provider=redis_provider)
        set_sandbox_proxy_service(sandbox_manager)

    logger.info("rock-admin start")

    yield

    # stop scheduler process
    if scheduler_process:
        scheduler_process.stop()
        logger.info("Scheduler process stopped")

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

    request_json = dict(request.query_params)
    if request.headers.get("content-type", "").lower().startswith("application/json"):
        try:
            request_json = await request.json()
        except Exception as e:
            req_logger.error(f"Could not decode request body:{request_json}, error:{e}")
            request_json = {}

        # Get SANDBOX_ID from JSON field
    sandbox_id = request_json.get("sandbox_id")
    if sandbox_id is not None:
        sandbox_id_ctx_var.set(sandbox_id)

    trace_id = request.headers.get(EAGLE_EYE_TRACE_ID) if request.headers.get(EAGLE_EYE_TRACE_ID) else uuid.uuid4().hex
    trace_id_ctx_var.set(trace_id)
    request_data = {
        "access_type": "request",
        "method": request.method,
        "url": str(request.url),
        "headers": dict(request.headers),
        "request_content": request_json,
    }
    req_logger.info(json.dumps(request_data, indent=2))

    # Process request and log response
    start_time = time.perf_counter()
    response = await call_next(request)
    process_time = f"{(time.perf_counter() - start_time) * 1000:.2f}ms"

    response_data = {
        "access_type": "response",
        "status_code": response.status_code,
        "process_time": process_time,
    }
    req_logger.info(json.dumps(response_data, indent=2))

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
