from typing import Any

from fastapi import APIRouter, Request

proxy_router = APIRouter()


@proxy_router.post("/v1/chat/completions")
async def chat_completions(body: dict[str, Any], request: Request):
    raise NotImplementedError("Proxy chat completions not implemented yet")
