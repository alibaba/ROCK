"""LLM Service - FastAPI server for sandbox communication."""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from rock.sdk.model.server.config import LOG_FILE, SERVICE_HOST, SERVICE_PORT
from rock.sdk.model.server.file_handler import FileHandler

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Global request counter
request_counter = 0
file_handler: FileHandler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager."""
    global file_handler

    # Delete old log file if exists and create new one
    if Path(LOG_FILE).exists():
        Path(LOG_FILE).unlink()
        logger.info(f"Deleted old log file: {LOG_FILE}")

    # Create new log file
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(LOG_FILE).touch()
    logger.info(f"Created new log file: {LOG_FILE}")

    file_handler = FileHandler()
    logger.info("LLM Service started")
    yield
    logger.info("LLM Service shutting down")


# Create FastAPI app
app = FastAPI(
    title="LLM Service",
    description="Sandbox LLM Service for Agent and Roll communication",
    version="1.0.0",
    lifespan=lifespan,
)


def get_next_request_index() -> int:
    """Get next request index."""
    global request_counter
    request_counter += 1
    return request_counter


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.post("/v1/agent/watch")
async def start_watch_agent(body: dict[str, Any], request: Request):
    agent_pid = body.get("pid")
    if agent_pid is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing 'pid' in request body")
    logger.info(f"Start watching agent process with pid: {agent_pid}")

    async def is_process_alive(pid: int) -> bool:
        if not psutil.pid_exists(pid):
            return False
        try:
            p = psutil.Process(pid)
            return p.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    # submit a background task to watch the agent process
    async def watch_agent_process(pid: int):
        while True:
            if not await is_process_alive(pid):
                logger.info(f"Agent process with pid {pid} has exited. Sending SESSION_END.")
                file_handler.write_session_end()
                break
            else:
                logger.info(f"Agent process with pid {pid} is still running.")
            await asyncio.sleep(5)  # check every 5 seconds

    asyncio.create_task(watch_agent_process(agent_pid))
    return {"status": "watching", "pid": agent_pid}


@app.post("/v1/chat/completions")
async def chat_completions(body: dict[str, Any], request: Request):
    """
    OpenAI-compatible chat completions endpoint.

    Handles both streaming and non-streaming requests.
    Accepts any JSON payload without strict validation.
    """
    request_id = None
    request_index = None

    try:
        # Generate request ID and index
        request_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
        request_index = get_next_request_index()

        logger.info(f"Received request {request_id} (index: {request_index})")

        # Add metadata to request
        request_dict = dict(body)
        # Write request to file
        file_handler.write_request(request_dict, request_index)

        # Poll for response from Roll (async, allows handling other requests)
        response_data = await file_handler.poll_for_response(request_index)

        if response_data is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="No response received from Roll process"
            )

        # Return response data as-is from Roll, no transformation
        return response_data

    except HTTPException:
        raise
    except asyncio.CancelledError:
        logger.info(f"Request {request_index} was cancelled")
        raise HTTPException(
            status_code=499,  # Non-standard but commonly used for client closed request
            detail="Request cancelled",
        )
    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error: {str(e)}"
        )


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": {"message": str(exc), "type": "internal_error", "code": "internal_error"}},
    )


if __name__ == "__main__":
    logger.info(f"Starting LLM Service on {SERVICE_HOST}:{SERVICE_PORT}")
    uvicorn.run("main:app", host=SERVICE_HOST, port=SERVICE_PORT, log_level="info", reload=False)
