from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from rock.logger import init_logger
from rock.sdk.model.server.config import ModelServiceConfig
from rock.utils import retry_async

logger = init_logger(__name__)

proxy_router = APIRouter()


# Global HTTP client with a persistent connection pool
http_client = httpx.AsyncClient()


@retry_async(
    max_attempts=6,
    delay_seconds=2.0,
    backoff=2.0, # Exponential backoff (2s, 4s, 8s, 16s, 32s).
    jitter=True, # Adds randomness to prevent "thundering herd" effect on the backend.
    exceptions=(httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError)
)
async def perform_llm_request(url: str, body: dict, headers: dict, config: ModelServiceConfig):
    """
    Forwards the request and triggers retry ONLY if the status code 
    is in the explicit retryable whitelist.
    """
    response = await http_client.post(url, json=body, headers=headers, timeout=config.request_timeout)
    status_code = response.status_code

    # Check against the explicit whitelist
    if status_code in config.retryable_status_codes:
        logger.warning(f"Retryable error detected: {status_code}. Triggering retry for {url}...")
        response.raise_for_status()

    return response


def get_base_url(model_name: str, config: ModelServiceConfig) -> str:
    """
    Selects the target backend URL based on model name matching.
    """
    if not model_name:
        raise HTTPException(status_code=400, detail="Model name is required for routing.")

    rules = config.proxy_rules
    base_url = rules.get(model_name) or rules.get("default")
    if not base_url:
        raise HTTPException(status_code=400, detail=f"Model '{model_name}' is not configured and no 'default' rule found.")

    return base_url.rstrip("/")


@proxy_router.post("/v1/chat/completions")
async def chat_completions(body: dict[str, Any], request: Request):
    """
    OpenAI-compatible chat completions proxy endpoint.
    Handles routing, header transparent forwarding, and automatic retries.
    """
    config = request.app.state.model_service_config

    # Step 1: Model Routing
    model_name = body.get("model", "")
    base_url = get_base_url(model_name, config)
    target_url = f"{base_url}/chat/completions"
    logger.info(f"Routing model '{model_name}' to URL: {target_url}")

    # Step 2: Header Cleaning
    # Preserve 'Authorization' for authentication while removing hop-by-hop transport headers.
    forwarded_headers = {}
    for key, value in request.headers.items():
        if key.lower() in ["host", "content-length", "content-type", "transfer-encoding"]:
            continue
        forwarded_headers[key] = value

    # Step 3: Strategy Enforcement
    # Force non-streaming mode for the MVP phase to ensure stability.
    body["stream"] = False

    try:
        # Step 4: Execute Request with Retry Logic
        response = await perform_llm_request(target_url, body, forwarded_headers, config)
        return JSONResponse(status_code=response.status_code, content=response.json())

    except httpx.HTTPStatusError as e:
        # Forward the raw backend error message to the client.
        # This allows the Agent-side logic to detect keywords like 'context length exceeded'
        # or 'content violation' and raise appropriate exceptions.
        error_text = e.response.text if e.response else "No error details"
        status_code = e.response.status_code if e.response else 502
        logger.error(f"Final failure after retries. Status: {status_code}, Response: {error_text}")
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "message": f"LLM backend error: {error_text}",
                    "type": "proxy_retry_failed",
                    "code": status_code
                }
            }
        )
    except Exception as e:
        logger.error(f"Unexpected proxy error: {str(e)}")
        # Raise standard 500 for non-HTTP related coding or system errors
        raise HTTPException(status_code=500, detail=str(e))
