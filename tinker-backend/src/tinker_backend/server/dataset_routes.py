"""Task listing endpoints backed by public datasets."""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException

from tinker_backend.protocol.schemas import ListTasksRequest, ListTasksResponse
from tinker_backend.services.task_catalog import list_task_descriptors

router = APIRouter()


@router.post("/api/v1/list_tasks", response_model=ListTasksResponse)
async def list_tasks(body: ListTasksRequest) -> ListTasksResponse:
    try:
        tasks = await asyncio.to_thread(list_task_descriptors, body.datasets)
    except re.error as exc:
        raise HTTPException(status_code=400, detail=f"Invalid task_filter regex: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid task catalog request: {exc}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"Task catalog unavailable: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list tasks: {exc}") from exc
    return ListTasksResponse(tasks=tasks, total=len(tasks))
