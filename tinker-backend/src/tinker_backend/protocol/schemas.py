from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SUPPORTED_RUNTIME_TYPES = {"roll", "verl", "dummy"}
SUPPORTED_CONFIG_TYPES = {"yaml"}
def normalize_runtime_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in SUPPORTED_RUNTIME_TYPES:
        allowed = ", ".join(sorted(SUPPORTED_RUNTIME_TYPES))
        raise ValueError(f"runtime_type must be one of: {allowed}")
    return normalized


def normalize_config_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in SUPPORTED_CONFIG_TYPES:
        allowed = ", ".join(sorted(SUPPORTED_CONFIG_TYPES))
        raise ValueError(f"config_type must be one of: {allowed}")
    return normalized



class HealthResponse(BaseModel):
    status: str = "ok"


class ClientConfigRequest(BaseModel):
    type: str | None = None


class ClientConfigResponse(BaseModel):
    pjwt_auth_enabled: bool = False
    credential_default_source: str = "api_key"
    sample_dispatch_bytes_semaphore_size: int = 10 * 1024 * 1024
    inflight_response_bytes_semaphore_size: int = 50 * 1024 * 1024


class CreateSessionRequest(BaseModel):
    tags: list[str] = Field(default_factory=list)
    user_metadata: dict[str, Any] | None = None
    sdk_version: str = "unknown"
    project_id: str | None = None
    type: Literal["create_session"] = "create_session"


class CreateSessionResponse(BaseModel):
    type: Literal["create_session"] = "create_session"
    info_message: str | None = None
    warning_message: str | None = None
    error_message: str | None = None
    session_id: str


class SessionHeartbeatRequest(BaseModel):
    session_id: str
    type: Literal["session_heartbeat"] = "session_heartbeat"


class SessionHeartbeatResponse(BaseModel):
    type: Literal["session_heartbeat"] = "session_heartbeat"


class CreateRuntimeRequest(BaseModel):
    runtime_type: str
    config_type: str = "yaml"
    config_content: str
    session_id: str | None = None
    user_metadata: dict[str, Any] | None = None
    type: Literal["create_runtime"] = "create_runtime"

    @field_validator("runtime_type")
    @classmethod
    def validate_runtime_type(cls, value: str) -> str:
        return normalize_runtime_type(value)

    @field_validator("config_type")
    @classmethod
    def validate_config_type(cls, value: str) -> str:
        return normalize_config_type(value)

    @field_validator("config_content")
    @classmethod
    def validate_config_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("config_content must not be empty")
        return value


class FutureResponse(BaseModel):
    type: str
    future_id: str
    request_id: str
    status: str
    runtime_id: str | None = None
    model_id: str | None = None


class CreateRuntimeOutput(BaseModel):
    type: Literal["create_runtime"] = "create_runtime"
    runtime_id: str
    runtime_type: str
    status: str
    ready: bool
    config_type: str
    config_path: str | None = None
    adapter_base_url: str | None = None
    error_message: str | None = None


class RuntimeStatusResponse(BaseModel):
    runtime_id: str
    runtime_type: str
    status: str
    ready: bool
    config_type: str
    config_path: str | None = None
    adapter_base_url: str | None = None
    error_message: str | None = None
    session_id: str | None = None


class CloseRuntimeRequest(BaseModel):
    force_after_seconds: float | None = Field(default=None, ge=0)
    type: Literal["close_runtime"] = "close_runtime"


class CloseRuntimeOutput(BaseModel):
    type: Literal["close_runtime"] = "close_runtime"
    runtime_id: str
    status: str
    graceful: bool
    killed_pids: list[int] = Field(default_factory=list)
    message: str | None = None


class RuntimeHeartbeatRequest(BaseModel):
    runtime_id: str | None = None
    status: str = "ready"
    ready: bool = False
    adapter_base_url: str | None = None
    process_pid: int | None = None
    error_message: str | None = None
    metadata: dict[str, Any] | None = None
    type: Literal["runtime_heartbeat"] = "runtime_heartbeat"


class RuntimeHeartbeatResponse(BaseModel):
    ok: bool = True
    runtime_id: str
    status: str
    ready: bool


class FutureRetrieveRequest(BaseModel):
    request_id: str
    allow_metadata_only: bool = True


class ServerCapabilitiesResponse(BaseModel):
    supported_models: list[dict[str, Any]] = Field(default_factory=list)
    supported_runtime_types: list[str] = Field(default_factory=lambda: sorted(SUPPORTED_RUNTIME_TYPES))
    supported_config_types: list[str] = Field(default_factory=lambda: sorted(SUPPORTED_CONFIG_TYPES))
    features: list[str] = Field(default_factory=lambda: [
        "runtime_create",
        "runtime_route_params",
        "runtime_close",
        "pg_only_futures",
        "action_claim",
        "task_env_future",
        "runtime_scoped_steps",
        "runtime_scoped_sampling",
        "runtime_scoped_training",
        "runtime_scoped_publish",
    ])


class ClaimActionsRequest(BaseModel):
    action_types: list[str] | None = None
    limit: int = Field(default=10, ge=1, le=100)


class ClaimedAction(BaseModel):
    action_id: int
    action_type: str
    env_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ClaimActionsResponse(BaseModel):
    actions: list[ClaimedAction] = Field(default_factory=list)


class PostActionResultRequest(BaseModel):
    status: Literal["completed", "failed"]
    result_data: dict[str, Any] | None = None
    error_message: str | None = None


class PostActionResultResponse(BaseModel):
    ok: bool = True


class OpenAIChatPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["openai_chat_completion_request"] = "openai_chat_completion_request"
    version: Literal[1] = 1
    request: dict[str, Any]

    @field_validator("request")
    @classmethod
    def validate_openai_chat_request(cls, value: dict[str, Any]) -> dict[str, Any]:
        messages = value.get("messages")
        if not isinstance(messages, list):
            raise ValueError("OpenAI chat prompt request must include a messages list")
        return value

class PostStepRequest(BaseModel):
    step_id: int
    prompt: OpenAIChatPrompt | None = None
    finish_reason: str | None = None
    reward: float | None = None


class PostStepResponse(BaseModel):
    ok: bool = True


class DatasetSpec(BaseModel):
    dataset: str
    split: str
    bench_name: str
    task_filter: str | None = None


class TaskDescriptor(BaseModel):
    task_id: str
    dataset: str
    split: str
    bench_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ListTasksRequest(BaseModel):
    datasets: list[DatasetSpec] = Field(min_length=1)


class ListTasksResponse(BaseModel):
    tasks: list[TaskDescriptor] = Field(default_factory=list)
    total: int = 0


class InitTaskEnvRequest(BaseModel):
    task_id: str
    dataset: str
    split: str
    metadata: dict[str, Any] | None = None


class GetStepRequest(BaseModel):
    env_id: str
    step_id: int | None = None


class GetStepResponse(BaseModel):
    step_id: int = 0
    prompt: OpenAIChatPrompt | None = None
    finish_reason: str | None = None
    reward: float | None = None
    warning_message: str | None = None


class SampleRequest(BaseModel):
    env_id: str | None = None
    prompt: OpenAIChatPrompt | None = None
    model_input: dict[str, Any] | None = None
    sampling_params: dict[str, Any] | None = None
    num_samples: int = 1
    model_id: str | None = None
    base_model: str | None = None
    model_path: str | None = None
    sampling_session_id: str | None = None
    seq_id: int | None = None
    prompt_logprobs: bool | None = None
    topk_prompt_logprobs: int = 0

    @field_validator("model_input")
    @classmethod
    def validate_model_input(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and not isinstance(value.get("chunks"), list):
            raise ValueError("model_input must include a chunks list")
        return value

    @model_validator(mode="after")
    def validate_exactly_one_input(self) -> "SampleRequest":
        has_prompt = self.prompt is not None
        has_model_input = self.model_input is not None
        if has_prompt == has_model_input:
            raise ValueError("Exactly one of prompt or model_input must be provided")
        return self


class CreateModelRequest(BaseModel):
    session_id: str | None = None
    model_seq_id: int | None = None
    base_model: str
    lora_config: dict[str, Any] | None = None
    user_metadata: dict[str, Any] | None = None
    type: Literal["create_model"] = "create_model"


class CreateModelOutput(BaseModel):
    type: Literal["create_model"] = "create_model"
    model_id: str
    base_model: str
    lora_config: dict[str, Any] | None = None
    status: str = "created"


class CreateSamplingSessionRequest(BaseModel):
    session_id: str | None = None
    sampling_session_seq_id: int | None = None
    base_model: str | None = None
    model_path: str | None = None
    type: Literal["create_sampling_session"] = "create_sampling_session"

    @model_validator(mode="after")
    def validate_source(self) -> "CreateSamplingSessionRequest":
        if (self.base_model is None) == (self.model_path is None):
            raise ValueError("Exactly one of base_model or model_path must be provided")
        return self


class CreateSamplingSessionOutput(BaseModel):
    type: Literal["create_sampling_session"] = "create_sampling_session"
    sampling_session_id: str


class ForwardBackwardRequest(BaseModel):
    model_id: str
    forward_backward_input: dict[str, Any] | None = None
    data: list[dict[str, Any]] = Field(default_factory=list)
    loss_fn: str | None = "cross_entropy"
    loss_fn_config: dict[str, float] | None = None
    seq_id: int | None = None

    @model_validator(mode="after")
    def normalize_input(self) -> "ForwardBackwardRequest":
        if self.forward_backward_input is None:
            self.forward_backward_input = {
                "data": self.data,
                "loss_fn": self.loss_fn or "cross_entropy",
                "loss_fn_config": self.loss_fn_config,
            }
        return self


class ForwardRequest(BaseModel):
    model_id: str
    forward_input: dict[str, Any] | None = None
    data: list[dict[str, Any]] = Field(default_factory=list)
    loss_fn: str | None = "cross_entropy"
    loss_fn_config: dict[str, float] | None = None
    seq_id: int | None = None

    @model_validator(mode="after")
    def normalize_input(self) -> "ForwardRequest":
        if self.forward_input is None:
            self.forward_input = {
                "data": self.data,
                "loss_fn": self.loss_fn or "cross_entropy",
                "loss_fn_config": self.loss_fn_config,
            }
        return self


class OptimStepRequest(BaseModel):
    model_id: str
    adam_params: dict[str, Any] | None = None
    seq_id: int | None = None


class SaveWeightsRequest(BaseModel):
    model_id: str
    path: str | None = None
    seq_id: int | None = None
    ttl_seconds: int | None = None


class PublishToSamplerRequest(BaseModel):
    model_id: str
    path: str | None = None
    sampling_session_seq_id: int | None = None
    seq_id: int | None = None
    ttl_seconds: int | None = None
    type: Literal["publish_to_sampler"] = "publish_to_sampler"


class PublishToSamplerOutput(BaseModel):
    path: str | None = None
    sampling_session_id: str | None = None
    type: Literal["publish_to_sampler"] = "publish_to_sampler"


class LoadWeightsRequest(BaseModel):
    model_id: str
    path: str = ""
    optimizer: bool = False
    seq_id: int | None = None
    weights_access_token: str | None = None
