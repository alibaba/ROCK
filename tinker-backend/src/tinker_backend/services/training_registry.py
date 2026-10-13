"""Runtime-scoped training registry helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from tinker_backend.protocol.enums import FutureState
from tinker_backend.protocol.schemas import (
    CreateModelRequest,
    CreateSamplingSessionRequest,
    LoadWeightsRequest,
    PublishToSamplerRequest,
    SaveWeightsRequest,
)
from tinker_backend.storage.models import (
    CheckpointRecord,
    ClientSessionRecord,
    OperationFutureRecord,
    RuntimeActionRecord,
    SamplingSessionRecord,
    TrainingModelRecord,
    utcnow,
)


@dataclass(frozen=True)
class ParsedTinkerPath:
    primary_id: str
    kind: str
    secondary_id: str


def make_model_id() -> str:
    return "model_" + uuid4().hex[:8]


def make_sampling_session_id() -> str:
    return "sampling_" + uuid4().hex[:8]


def parse_tinker_path(value: str) -> ParsedTinkerPath | None:
    parsed = urlparse(value)
    parts = parsed.path.split("/")
    if parsed.scheme != "tinker":
        return None
    if len(parts) == 2 and parts[0] == "":
        return ParsedTinkerPath(primary_id=parsed.netloc, kind="", secondary_id=parts[1])
    if len(parts) == 3 and parts[0] == "":
        return ParsedTinkerPath(primary_id=parsed.netloc, kind=parts[1], secondary_id=parts[2])
    return None


async def validate_session_if_present(session: AsyncSession, session_id: str | None) -> None:
    if session_id is not None and await session.get(ClientSessionRecord, session_id) is None:
        raise ValueError("Session not found")


async def require_model(
    session: AsyncSession,
    *,
    runtime_id: str,
    model_id: str,
) -> TrainingModelRecord:
    model = await session.get(TrainingModelRecord, model_id)
    if model is None:
        raise ValueError("Model not found")
    if model.runtime_id != runtime_id:
        raise ValueError("Model does not belong to this runtime")
    return model


async def create_model_record(
    session: AsyncSession,
    *,
    runtime_id: str,
    request: CreateModelRequest,
) -> TrainingModelRecord:
    await validate_session_if_present(session, request.session_id)
    model = TrainingModelRecord(
        model_id=make_model_id(),
        runtime_id=runtime_id,
        session_id=request.session_id,
        model_seq_id=request.model_seq_id,
        base_model=request.base_model,
        lora_config=request.lora_config,
        user_metadata=request.user_metadata,
        status="pending_create",
    )
    session.add(model)
    await session.flush()
    return model


async def create_sampling_session_record(
    session: AsyncSession,
    *,
    runtime_id: str,
    request: CreateSamplingSessionRequest,
) -> SamplingSessionRecord:
    await validate_session_if_present(session, request.session_id)
    model_id: str | None = None
    checkpoint_id: str | None = None
    base_model = request.base_model
    if request.model_path:
        parsed = parse_tinker_path(request.model_path)
        if parsed is None:
            raise ValueError("model_path must be a tinker:// path")
        model_id = parsed.primary_id
        checkpoint_id = parsed.secondary_id
        model = await require_model(session, runtime_id=runtime_id, model_id=model_id)
        base_model = model.base_model
    sampling = SamplingSessionRecord(
        sampling_session_id=make_sampling_session_id(),
        runtime_id=runtime_id,
        session_id=request.session_id,
        sampling_session_seq_id=request.sampling_session_seq_id,
        base_model=base_model,
        model_path=request.model_path,
        model_id=model_id,
        checkpoint_id=checkpoint_id,
    )
    session.add(sampling)
    await session.flush()
    return sampling


async def resolve_sampling_payload(
    session: AsyncSession,
    *,
    runtime_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Attach model/checkpoint metadata needed by runtime-side sampler workers."""
    resolved = dict(payload)
    sampling_session_id = resolved.get("sampling_session_id")

    if sampling_session_id:
        sampling = await session.get(SamplingSessionRecord, sampling_session_id)
        if sampling is None:
            # Runtime.sample() in the rollout path historically used runtime_id as
            # a synthetic sampling_session_id. Keep that direct path transparent,
            # while treating real generated sampling ids as registry misses.
            if str(sampling_session_id).startswith("sampling_"):
                raise ValueError("Sampling session not found")
        else:
            if sampling.runtime_id != runtime_id:
                raise ValueError("Sampling session does not belong to this runtime")
            if sampling.base_model:
                body_base_model = resolved.get("base_model")
                if body_base_model and body_base_model != sampling.base_model:
                    raise ValueError("base_model conflicts with sampling session")
                resolved["base_model"] = sampling.base_model
            if sampling.model_path:
                body_model_path = resolved.get("model_path")
                if body_model_path and body_model_path != sampling.model_path:
                    raise ValueError("model_path conflicts with sampling session")
                resolved["model_path"] = sampling.model_path
            if sampling.model_id:
                body_model_id = resolved.get("model_id")
                if body_model_id and body_model_id != sampling.model_id:
                    raise ValueError("model_id conflicts with sampling session")
                resolved["model_id"] = sampling.model_id
            if sampling.checkpoint_id:
                resolved["checkpoint_id"] = resolved.get("checkpoint_id") or sampling.checkpoint_id

    model_path = resolved.get("model_path")
    if model_path:
        parsed = parse_tinker_path(str(model_path))
        if parsed is None:
            raise ValueError("model_path must be a tinker:// path")
        body_model_id = resolved.get("model_id")
        if body_model_id and body_model_id != parsed.primary_id:
            raise ValueError("model_id conflicts with model_path")
        resolved["model_id"] = parsed.primary_id
        resolved["checkpoint_id"] = resolved.get("checkpoint_id") or parsed.secondary_id

    model_id = resolved.get("model_id")
    if model_id:
        await require_model(session, runtime_id=runtime_id, model_id=str(model_id))

    return resolved


async def create_checkpoint_record(
    session: AsyncSession,
    *,
    runtime_id: str,
    model_id: str,
    checkpoint_id: str,
    checkpoint_type: str,
    path: str | None,
) -> CheckpointRecord:
    await require_model(session, runtime_id=runtime_id, model_id=model_id)
    existing = await session.get(CheckpointRecord, (checkpoint_id, model_id, checkpoint_type))
    if existing is not None:
        raise ValueError(f"Checkpoint {checkpoint_id!r} already exists for model {model_id!r}")
    checkpoint = CheckpointRecord(
        checkpoint_id=checkpoint_id,
        model_id=model_id,
        checkpoint_type=checkpoint_type,
        runtime_id=runtime_id,
        status="pending",
        path=path,
    )
    session.add(checkpoint)
    await session.flush()
    return checkpoint


def checkpoint_id_for_publish(request: PublishToSamplerRequest) -> str:
    if request.path:
        return request.path.rsplit("/", 1)[-1]
    return f"ss{request.sampling_session_seq_id}_seq{request.seq_id}"


async def prepare_save_weights(
    session: AsyncSession,
    *,
    runtime_id: str,
    request: SaveWeightsRequest,
) -> CheckpointRecord:
    checkpoint_id = request.path
    if not checkpoint_id:
        raise ValueError("path is required for save_weights")
    return await create_checkpoint_record(
        session,
        runtime_id=runtime_id,
        model_id=request.model_id,
        checkpoint_id=checkpoint_id,
        checkpoint_type="training",
        path=f"tinker://{request.model_id}/weights/{checkpoint_id}",
    )


async def prepare_publish_to_sampler(
    session: AsyncSession,
    *,
    runtime_id: str,
    request: PublishToSamplerRequest,
) -> tuple[CheckpointRecord, SamplingSessionRecord | None]:
    model = await require_model(session, runtime_id=runtime_id, model_id=request.model_id)
    checkpoint_id = checkpoint_id_for_publish(request)
    sampling: SamplingSessionRecord | None = None
    model_path = f"tinker://{request.model_id}/sampler_weights/{checkpoint_id}"
    if request.sampling_session_seq_id is not None and request.seq_id is not None:
        sampling = SamplingSessionRecord(
            sampling_session_id=make_sampling_session_id(),
            runtime_id=runtime_id,
            session_id=model.session_id,
            sampling_session_seq_id=request.sampling_session_seq_id,
            base_model=model.base_model,
            model_path=model_path,
            model_id=request.model_id,
            checkpoint_id=checkpoint_id,
        )
        session.add(sampling)
    checkpoint = await create_checkpoint_record(
        session,
        runtime_id=runtime_id,
        model_id=request.model_id,
        checkpoint_id=checkpoint_id,
        checkpoint_type="sampler",
        path=model_path,
    )
    return checkpoint, sampling


def load_weights_payload(request: LoadWeightsRequest) -> dict[str, Any]:
    parsed = parse_tinker_path(request.path)
    if parsed is None or parsed.kind != "weights":
        raise ValueError("path must be in format tinker://source_model_id/weights/checkpoint_id")
    return {
        "model_id": request.model_id,
        "source_model_id": parsed.primary_id,
        "checkpoint_id": parsed.secondary_id,
        "optimizer": request.optimizer,
        "seq_id": request.seq_id,
        "weights_access_token": request.weights_access_token,
        "path": request.path,
    }


def attach_future(
    future: OperationFutureRecord,
    *,
    model_id: str | None = None,
) -> None:
    if model_id is not None:
        future.model_id = model_id


def normalize_action_result(action: RuntimeActionRecord, result_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if result_data is None:
        result: dict[str, Any] = {}
    else:
        result = dict(result_data)

    payload = action.payload if isinstance(action.payload, dict) else {}
    if action.action_type == "create_model":
        result.setdefault("type", "create_model")
        result.setdefault("model_id", payload.get("model_id"))
        result.setdefault("base_model", payload.get("base_model"))
        result.setdefault("lora_config", payload.get("lora_config"))
    elif action.action_type == "publish_to_sampler":
        result.setdefault("type", "publish_to_sampler")
        result.setdefault("sampling_session_id", payload.get("sampling_session_id"))
        if "path" not in result:
            if payload.get("sampling_session_id") is not None:
                result["path"] = None
            elif payload.get("model_id") and payload.get("checkpoint_id"):
                result["path"] = f"tinker://{payload['model_id']}/sampler_weights/{payload['checkpoint_id']}"
            else:
                result["path"] = payload.get("path")
    elif action.action_type == "save_weights":
        result.setdefault("type", "save_weights")
        if "path" not in result and payload.get("model_id") and payload.get("checkpoint_id"):
            result["path"] = f"tinker://{payload['model_id']}/weights/{payload['checkpoint_id']}"
    elif action.action_type == "load_weights":
        result.setdefault("type", "load_weights")

    return result if result or result_data is not None else None


async def mark_action_records(
    session: AsyncSession,
    *,
    action: RuntimeActionRecord,
    status: str,
    result_data: dict[str, Any] | None,
    error_message: str | None,
) -> None:
    model_id = action.payload.get("model_id") if isinstance(action.payload, dict) else None
    now = utcnow()

    if action.action_type == "create_model" and model_id:
        model = await session.get(TrainingModelRecord, model_id)
        if model is not None:
            model.status = "ready" if status == "completed" else "failed"
            model.updated_at = now

    checkpoint_type = None
    if action.action_type == "save_weights":
        checkpoint_type = "training"
    elif action.action_type == "publish_to_sampler":
        checkpoint_type = "sampler"
    if checkpoint_type and model_id:
        checkpoint_id = action.payload.get("checkpoint_id") if isinstance(action.payload, dict) else None
        if checkpoint_id:
            checkpoint = await session.get(CheckpointRecord, (checkpoint_id, model_id, checkpoint_type))
            if checkpoint is not None:
                checkpoint.status = "completed" if status == "completed" else "failed"
                checkpoint.completed_at = now
                checkpoint.error_message = error_message
                if result_data and isinstance(result_data.get("path"), str):
                    checkpoint.path = result_data["path"]

    if status == "failed" and action.future_id is not None:
        future = await session.get(OperationFutureRecord, action.future_id)
        if future is not None and future.status == FutureState.FAILED.value:
            future.error_message = error_message or future.error_message
