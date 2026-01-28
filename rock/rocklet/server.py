#!/usr/bin/env python3

import argparse
import asyncio
import json
import time
import traceback
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_504_GATEWAY_TIMEOUT

from rock.actions import _ExceptionTransfer
from rock.logger import init_logger
from rock.rocklet import __version__
from rock.rocklet.local_api import local_router
from rock.utils import EAGLE_EYE_TRACE_ID, REQUEST_TIMEOUT_SECONDS, sandbox_id_ctx_var, trace_id_ctx_var

logger = init_logger("rocklet.server")
app = FastAPI()

app.include_router(local_router, tags=["local"])


@app.middleware("http")
async def log_requests_and_responses(request: Request, call_next):
    if request.url.path.startswith("/SandboxFusion"):
        return await call_next(request)

    req_logger = init_logger("rocklet.accessLog", "access.log")
    # Record request information
    request_json = dict(request.query_params)
    if request.headers.get("content-type", "").lower().startswith("application/json"):
        try:
            request_json = await request.json()
        except Exception as e:
            req_logger.error(f"Could not decode request body:{request_json}, error:{e}")
            request_json = {}

    # Extract sandbox_id
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

    # Process request and record response
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


@app.middleware("http")
async def timeout_middleware(request: Request, call_next):
    try:
        return await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        msg = f"Request processing timed out after {REQUEST_TIMEOUT_SECONDS} seconds."
        logger.error(msg)
        return JSONResponse(status_code=HTTP_504_GATEWAY_TIMEOUT, content={"failure_reason": msg, "exit_code": 124})


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
