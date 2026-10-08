import typing as _t

from . import types
from ._base_client import DefaultAioHttpClient, DefaultAsyncHttpxClient
from ._client import AsyncTinker, RequestOptions, Timeout
from ._exceptions import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    RequestFailedError,
    SidecarDiedError,
    SidecarError,
    SidecarIPCError,
    SidecarStartupError,
    TinkerError,
    UnprocessableEntityError,
)
from ._response import APIResponse as APIResponse
from ._response import AsyncAPIResponse as AsyncAPIResponse
from ._utils._logs import setup_logging as _setup_logging
from ._version import __title__, __version__
from .task_catalog import PublicTaskCatalog
from .lib.public_interfaces import (
    APIFuture,
    RuntimeClient,
    SamplingClient,
    TinkerClient,
    TrainingClient,
)
from .server_job import ServerJob

# Import commonly used types for easier access
from .types import (
    AdamParams,
    Checkpoint,
    CheckpointType,
    CreateRuntimeResponse,
    DatasetSpec,
    Datum,
    EncodedTextChunk,
    ForwardBackwardOutput,
    LoraConfig,
    ModelID,
    ModelInput,
    ModelInputChunk,
    OpenAIChatPrompt,
    OptimStepRequest,
    OptimStepResponse,
    ParsedCheckpointTinkerPath,
    RuntimeGetStepResponse,
    RuntimeSampleRequest,
    SampledSequence,
    SampleRequest,
    SampleResponse,
    SamplingParams,
    StopReason,
    TaskDescriptor,
    TensorData,
    TensorDtype,
    TrainingRun,
)

__all__ = [
    # Core clients
    "AsyncTinker",
    "TrainingClient",
    "TinkerClient",
    "ServerJob",
    "SamplingClient",
    "RuntimeClient",
    "APIFuture",
    "PublicTaskCatalog",
    # Commonly used types
    "AdamParams",
    "Checkpoint",
    "CheckpointType",
    "CreateRuntimeResponse",
    "DatasetSpec",
    "Datum",
    "EncodedTextChunk",
    "ForwardBackwardOutput",
    "LoraConfig",
    "ModelID",
    "ModelInput",
    "ModelInputChunk",
    "OpenAIChatPrompt",
    "OptimStepRequest",
    "OptimStepResponse",
    "ParsedCheckpointTinkerPath",
    "SampledSequence",
    "SampleRequest",
    "SampleResponse",
    "RuntimeGetStepResponse",
    "RuntimeSampleRequest",
    "SamplingParams",
    "StopReason",
    "TensorData",
    "TensorDtype",
    "TaskDescriptor",
    "TrainingRun",
    # Client configuration
    "Timeout",
    "RequestOptions",
    "DefaultAsyncHttpxClient",
    "DefaultAioHttpClient",
    # Exception types
    "TinkerError",
    "APIError",
    "APIStatusError",
    "APITimeoutError",
    "APIConnectionError",
    "APIResponseValidationError",
    "RequestFailedError",
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    "SidecarError",
    "SidecarStartupError",
    "SidecarDiedError",
    "SidecarIPCError",
    # Keep types module for advanced use
    "types",
    # Version info
    "__version__",
    "__title__",
]

if not _t.TYPE_CHECKING:
    from ._utils._resources_proxy import resources as resources

_setup_logging()

# Update the __module__ attribute for exported symbols so that
# error messages point to this module instead of the module
# it was originally defined in, e.g.
# tinker._exceptions.NotFoundError -> tinker.NotFoundError
__locals = locals()
for __name in __all__:
    if not __name.startswith("__"):
        try:
            __locals[__name].__module__ = "tinker"
        except (TypeError, AttributeError):
            # Some of our exported symbols are builtins which we can't set attributes for.
            pass
