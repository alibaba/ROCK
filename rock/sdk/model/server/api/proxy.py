from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from rock.logger import init_logger
from rock.sdk.model.server.config import PROXY_TARGET_URL

logger = init_logger(__name__)

proxy_router = APIRouter()


async def forward_non_streaming_request(
    body: dict[str, Any], headers: dict[str, str], target_url: str
) -> tuple[Any, int]:
    """Forward non-streaming request to target API"""
    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"Forwarding non-streaming request body: {body}")
            logger.info(
                f"Forwarding headers: {['Authorization' if k.lower() == 'authorization' else k for k in headers.keys()] if headers else 'No headers'}"
            )

            # Use provided headers to forward the request
            response = await client.post(
                target_url,
                json=body,
                headers=headers,
                timeout=120.0,  # Set timeout to 60 seconds
            )

            logger.info(f"Target API non-streaming response status: {response.status_code}")

            # Try to parse the response as JSON
            try:
                response_data = response.json()
                logger.info(f"Target API non-streaming response data: {response_data}")
                return response_data, response.status_code
            except Exception:
                # If response is not JSON, return as text
                response_text = response.text
                logger.info(f"Target API non-streaming response text: {response_text}")
                return response_text, response.status_code

        except httpx.TimeoutException:
            logger.error("Request to target API timed out")
            raise HTTPException(status_code=504, detail="Request to target API timed out")
        except httpx.RequestError as e:
            logger.error(f"Error making non-streaming request to target API: {str(e)}")
            raise HTTPException(status_code=502, detail=f"Error contacting target API: {str(e)}")
        except Exception as e:
            logger.error(f"Unknown error making non-streaming request to target API: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Internal proxy error: {str(e)}")


async def forward_streaming_request(
    body: dict[str, Any], headers: dict[str, str], target_url: str
) -> StreamingResponse:
    """Forward streaming request to target API"""
    async with httpx.AsyncClient() as client:
        try:
            logger.info(f"Forwarding streaming request body: {body}")
            logger.info(
                f"Forwarding headers: {['Authorization' if k.lower() == 'authorization' else k for k in headers.keys()] if headers else 'No headers'}"
            )

            # Use provided headers to forward the request
            response = await client.post(
                target_url,
                json=body,
                headers=headers,
                timeout=120.0,  # Set timeout to 60 seconds
            )

            logger.info(f"Target API streaming response status: {response.status_code}")

            # Handle streaming response
            content_type = response.headers.get("content-type", "")

            async def generate():
                # Stream response data in chunks
                async for chunk in response.aiter_bytes():
                    yield chunk

            return StreamingResponse(generate(), media_type=content_type)

        except httpx.TimeoutException:
            logger.error("Request to target API timed out")
            raise HTTPException(status_code=504, detail="Request to target API timed out")
        except httpx.RequestError as e:
            logger.error(f"Error making streaming request to target API: {str(e)}")
            raise HTTPException(status_code=502, detail=f"Error contacting target API: {str(e)}")
        except Exception as e:
            logger.error(f"Unknown error making streaming request to target API: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Internal proxy error: {str(e)}")


@proxy_router.post("/v1/chat/completions")
async def chat_completions(body: dict[str, Any], request: Request):
    # Build forwarded headers while preserving original request headers
    forwarded_headers = {}
    for key, value in request.headers.items():
        # Copy all headers, but skip certain headers that httpx should set automatically
        if key.lower() in ["content-length", "content-type", "host", "transfer-encoding"]:
            continue  # Let httpx set these headers
        forwarded_headers[key] = value

    logger.info(f"Received request at proxy endpoint with body: {body}")

    # Determine target URL
    target_url = PROXY_TARGET_URL

    # Choose handler based on stream parameter
    if body.get("stream", False):
        # Forward streaming request
        result = await forward_streaming_request(body, forwarded_headers, target_url)
        return result
    else:
        # Forward non-streaming request
        response_data, status_code = await forward_non_streaming_request(body, forwarded_headers, target_url)

        if status_code == 200:
            return response_data
        else:
            return JSONResponse(content=response_data, status_code=status_code)
