#!/usr/bin/env python3

import argparse
import asyncio
import time
import traceback

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_504_GATEWAY_TIMEOUT

from rock.actions import _ExceptionTransfer
from rock.logger import init_logger
from rock.rocklet import __version__
from rock.rocklet.local_api import local_router
from rock.utils import REQUEST_TIMEOUT_SECONDS, ROUTE_KEY, SANDBOX_ID, sandbox_id_ctx_var

logger = init_logger("rocklet.server")
app = FastAPI()

app.include_router(local_router, tags=["local"])


@app.middleware("http")
async def log_requests_and_responses(request: Request, call_next):
    if request.url.path.startswith("/SandboxFusion"):
        return await call_next(request)

    if sandbox_id := (request.headers.get(SANDBOX_ID) or request.headers.get(ROUTE_KEY)):
        sandbox_id_ctx_var.set(sandbox_id)

    req_logger = init_logger("rocklet.accessLog")
    # Record request information
    req_logger.info(
        "request",
        extra={
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "sandbox_id": sandbox_id_ctx_var.get(),
        },
    )

    # Process request and record response
    start_time = time.perf_counter()
    response = await call_next(request)
    process_time = f"{(time.perf_counter() - start_time) * 1000:.2f}ms"

    req_logger.info(
        "response",
        extra={
            "status_code": response.status_code,
            "process_time": process_time,
            "sandbox_id": sandbox_id_ctx_var.get(),
        },
    )

    return response


@app.middleware("http")
async def timeout_middleware(request: Request, call_next):
    try:
        return await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        msg = f"Request processing timed out after {REQUEST_TIMEOUT_SECONDS} seconds."
        logger.error(msg)
        return JSONResponse(status_code=HTTP_504_GATEWAY_TIMEOUT, content={"detail": msg})


@app.exception_handler(Exception)
async def exception_handler(request: Request, exc: Exception):
    """We catch exceptions that are thrown by the runtime, serialize them to JSON and
    return them to the client so they can reraise them in their own code.
    """
    if isinstance(exc, HTTPException | StarletteHTTPException):
        return await http_exception_handler(request, exc)
    extra_info = getattr(exc, "extra_info", {})
    _exc = _ExceptionTransfer(
        message=str(exc),
        class_path=type(exc).__module__ + "." + type(exc).__name__,
        traceback=traceback.format_exc(),
        extra_info=extra_info,
    )
    return JSONResponse(status_code=511, content={"rockletexception": _exc.model_dump()})


@app.get("/")
async def root():
    return {"message": "hello world"}


def main():
    import uvicorn

    # First parser just for version checking
    version_parser = argparse.ArgumentParser(add_help=False)
    version_parser.add_argument("-v", "--version", action="store_true")
    version_args, remaining_args = version_parser.parse_known_args()

    if version_args.version:
        if remaining_args:
            print("Error: --version cannot be combined with other arguments")
            exit(1)
        print(__version__)
        return

    # Main parser for other arguments
    parser = argparse.ArgumentParser(description="Run the ROCKLET server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind the server to")
    parser.add_argument("--port", type=int, default=8000, help="Port to run the server on")

    args = parser.parse_args(remaining_args)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
