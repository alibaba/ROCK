import argparse
import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from rock import env_vars
from rock.admin.core.db_provider import DatabaseProvider
from rock.admin.core.ray_service import RayService
from rock.admin.core.sandbox_table import SandboxTable
from rock.admin.core.scheduler_task_table import SchedulerTaskTable
from rock.admin.entrypoints.admin_ops_api import admin_ops_router, set_ops_service
from rock.admin.entrypoints.sandbox_api import sandbox_router, set_sandbox_manager
from rock.admin.entrypoints.sandbox_proxy_api import sandbox_proxy_router, set_sandbox_proxy_service
from rock.admin.entrypoints.warmup_api import set_warmup_service, warmup_router
from rock.admin.gem.api import gem_router, set_env_service
from rock.admin.scheduler.scheduler import SchedulerThread, WorkerIPCache
from rock.admin.scheduler.task_base import BaseTask
from rock.admin.scheduler.task_factory import TaskFactory
from rock.admin.scheduler.tasks.sandbox_log_archive_task import (
    set_main_loop_provider as set_archive_main_loop_provider,
)
from rock.admin.scheduler.tasks.sandbox_log_archive_task import (
    set_rock_config_provider as set_archive_rock_config_provider,
)
from rock.admin.scheduler.tasks.sandbox_log_archive_task import (
    set_sandbox_table_provider as set_archive_sandbox_table_provider,
)
from rock.admin.service.ops_service import OpsService
from rock.common.exception import request_validation_exception_handler
from rock.config import DatabaseConfig, RockConfig, SchedulerConfig
from rock.logger import init_logger, reset_log_file
from rock.sandbox.gem_manager import GemManager
from rock.sandbox.operator.factory import OperatorContext, OperatorFactory
from rock.sandbox.sandbox_meta_store import SandboxMetaStore
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService
from rock.sandbox.service.warmup_service import WarmupService
from rock.utils import EAGLE_EYE_TRACE_ID, sandbox_id_ctx_var, trace_id_ctx_var
from rock.utils.providers import RedisProvider
from rock.utils.system import is_primary_pod
from rock.utils.worker import resolve_workers


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=str, default="local")
    parser.add_argument("--role", type=str, default="admin", choices=["admin", "proxy"])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--workers", type=int, default=None)
    return parser.parse_args()


logger = init_logger("admin")
logging.getLogger("urllib3").setLevel(logging.WARNING)


def _init_ops_service(
    rock_config: RockConfig,
    scheduler_task_table: "SchedulerTaskTable",
) -> OpsService:
    """Build OpsService with task registry from scheduler config."""
    ops_task_registry: dict[str, BaseTask] = {}
    if rock_config.scheduler.enabled:
        for task_config in rock_config.scheduler.tasks:
            if not getattr(task_config, "enabled", True):
                continue
            try:
                task = TaskFactory.create_task(task_config)
                ops_task_registry[task.type] = task
            except Exception as e:
                logger.warning(f"ops_taskset: failed to instantiate '{task_config.task_class}': {e}")

    ops_worker_cache = WorkerIPCache(cache_ttl=rock_config.scheduler.worker_cache_ttl)
    return OpsService(
        task_table=scheduler_task_table,
        task_registry=ops_task_registry,
        alive_workers_provider=ops_worker_cache.get_alive_workers,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_file_path = (
        Path(__file__).resolve().parents[2] / env_vars.ROCK_CONFIG_DIR_NAME / f"rock-{env_vars.ROCK_ADMIN_ENV}.yml"
        if not env_vars.ROCK_CONFIG
        else env_vars.ROCK_CONFIG
    )
    rock_config = RockConfig.from_env(config_file_path)

    # Override scheduler config from Nacos if available
    if rock_config.nacos_provider:
        nacos_config = await rock_config.nacos_provider.get_config()
        if nacos_config and "scheduler" in nacos_config:
            rock_config.scheduler = SchedulerConfig(**nacos_config["scheduler"])
            logger.info(f"Overrode scheduler config from Nacos with {len(rock_config.scheduler.tasks)} tasks")

    # init redis provider (fallback to fakeredis if no host configured)
    if env_vars.ROCK_ADMIN_ENV in ["local", "test", "dev"] or not rock_config.redis.host:
        from fakeredis import aioredis

        if not rock_config.redis.host:
            logger.info("redis.host is not configured, falling back to FakeRedis")
        redis_provider = RedisProvider(host=None, port=None, password="")
        redis_provider.client = aioredis.FakeRedis(decode_responses=True)
    else:
        redis_provider = RedisProvider(
            host=rock_config.redis.host,
            port=rock_config.redis.port,
            password=rock_config.redis.password,
        )
        await redis_provider.init_pool()

    # init database provider (fallback to sqlite in-memory if no url configured)
    db_url = rock_config.database.url or "sqlite+aiosqlite:///:memory:"
    if not rock_config.database.url:
        logger.info("database.url is not configured, falling back to SQLite in-memory")
    db_provider = DatabaseProvider(db_config=DatabaseConfig(url=db_url))
    await db_provider.init()
    if not rock_config.database.url:
        await db_provider.create_tables()
    sandbox_table = SandboxTable(db_provider, rock_config=rock_config)
    meta_store = SandboxMetaStore(redis_provider=redis_provider, sandbox_table=sandbox_table, rock_config=rock_config)

    # Wire SandboxLogArchiveTask deps. Providers (vs static set) so Nacos
    # config reload propagates to the next task run without re-injection.
    set_archive_sandbox_table_provider(lambda: sandbox_table)
    set_archive_rock_config_provider(lambda: rock_config)
    _main_loop = asyncio.get_running_loop()
    set_archive_main_loop_provider(lambda: _main_loop)

    # init scheduler task table (DB-backed, multi-pod safe)
    scheduler_task_table = SchedulerTaskTable(db_provider)

    # init scheduler thread
    scheduler_thread = None

    # init sandbox service
    proxy_service_ref = None
    if env_vars.ROCK_ADMIN_ROLE == "admin":
        # init ray service
        ray_service = RayService(rock_config.ray)
        ray_service.init()

        # create operator using factory with context pattern
        operator_context = OperatorContext(
            runtime_config=rock_config.runtime,
            ray_service=ray_service,
            redis_provider=redis_provider,
            nacos_provider=rock_config.nacos_provider,
            k8s_config=rock_config.k8s,
        )
        operator = OperatorFactory.create_operator(operator_context)

        # init service
        if rock_config.runtime.enable_auto_clear:
            sandbox_manager = GemManager(
                rock_config,
                ray_namespace=rock_config.ray.namespace,
                ray_service=ray_service,
                enable_runtime_auto_clear=True,
                operator=operator,
                meta_store=meta_store,
            )
        else:
            sandbox_manager = GemManager(
                rock_config,
                ray_namespace=rock_config.ray.namespace,
                ray_service=ray_service,
                enable_runtime_auto_clear=False,
                operator=operator,
                meta_store=meta_store,
            )
        set_sandbox_manager(sandbox_manager)
        warmup_service = WarmupService(rock_config.warmup)
        await warmup_service.init()
        set_warmup_service(warmup_service)
        set_env_service(sandbox_manager)

        if rock_config.scheduler.enabled and is_primary_pod():
            scheduler_thread = SchedulerThread(
                scheduler_config=rock_config.scheduler,
                nacos_provider=rock_config.nacos_provider,
            )
            scheduler_thread.start()
            logger.info("Scheduler thread started on primary pod")
        elif rock_config.scheduler.enabled:
            logger.info("Scheduler thread skipped on non-primary pod")

        set_ops_service(_init_ops_service(rock_config, scheduler_task_table))

    else:
        sandbox_manager = SandboxProxyService(rock_config=rock_config, meta_store=meta_store)
        set_sandbox_proxy_service(sandbox_manager)
        proxy_service_ref = sandbox_manager

    logger.info("rock-admin start")

    yield

    # stop scheduler thread
    if scheduler_thread:
        scheduler_thread.stop()
        logger.info("Scheduler thread stopped")

    if proxy_service_ref is not None:
        await proxy_service_ref.aclose()
        logger.info("proxy httpx clients closed")

    if db_provider:
        await db_provider.close()

    if redis_provider:
        await redis_provider.close_pool()

    logger.info("rock-admin exit")


async def base_exception_handler(request: Request, exc: Exception):
    exc_content = {"detail": str(exc), "traceback": traceback.format_exc().split("\n")}
    logger.error(f"[app error] request:[{request}], exc:[{exc_content}]")
    return JSONResponse(status_code=500, content=exc_content)


async def root():
    return {"message": "hello, ROCK!"}


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


def _include_routers(app: FastAPI, role: str) -> None:
    if role == "admin":
        app.include_router(sandbox_router, prefix="/apis/envs/sandbox/v1", tags=["sandbox"])
        app.include_router(admin_ops_router, prefix="/apis/envs/sandbox/v1/ops", tags=["admin-ops"])
    else:
        app.include_router(sandbox_proxy_router, prefix="/apis/envs/sandbox/v1", tags=["sandbox"])
    app.include_router(warmup_router, prefix="/apis/envs/sandbox/v1", tags=["warmup"])
    app.include_router(gem_router, prefix="/apis/v1/envs/gem", tags=["gem"])


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(Exception, base_exception_handler)
    app.middleware("http")(log_requests_and_responses)
    app.add_api_route("/", root, methods=["GET"])
    _include_routers(app, role=env_vars.ROCK_ADMIN_ROLE)
    return app


def main():
    args = _parse_args()
    os.environ["ROCK_ADMIN_ENV"] = args.env
    os.environ["ROCK_ADMIN_ROLE"] = args.role

    # Clear the log file once at deploy (master), then make every worker append
    # to the shared file. Must run before workers spawn so only the master clears.
    reset_log_file()
    os.environ["ROCK_LOGGING_APPEND"] = "true"

    workers = resolve_workers(
        role=args.role,
        override=args.workers,
        env_workers=int(os.getenv("ROCK_PROXY_WORKERS", "0")),
        env=args.env,
    )
    # write resolved count back so each worker's lifespan sizes pools deterministically
    os.environ["ROCK_PROXY_WORKERS"] = str(workers)
    logger.info(f"starting role={args.role} port={args.port} workers={workers}")

    uvicorn.run(
        "rock.admin.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=args.port,
        workers=workers,
        ws_ping_interval=None,
        ws_ping_timeout=None,
        timeout_keep_alive=30,
    )


if __name__ == "__main__":
    main()
