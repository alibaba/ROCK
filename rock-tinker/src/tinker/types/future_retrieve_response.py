# Derived from https://github.com/thinking-machines-lab/tinker (Apache-2.0).
# Modified in this Tinker fork; see rock-tinker/UPSTREAM.md and NOTICE.
from typing import Union

from typing_extensions import TypeAlias

from .close_runtime_response import CloseRuntimeResponse
from .create_model_response import CreateModelResponse
from .create_runtime_response import CreateRuntimeResponse
from .forward_backward_output import ForwardBackwardOutput
from .init_task_env_response import InitTaskEnvResponse
from .load_weights_response import LoadWeightsResponse
from .optim_step_response import OptimStepResponse
from .publish_to_sampler_response import PublishToSamplerResponse
from .request_failed_response import RequestFailedResponse
from .sample_response import SampleResponse
from .save_weights_for_sampler_response import SaveWeightsForSamplerResponse
from .save_weights_response import SaveWeightsResponse
from .try_again_response import TryAgainResponse
from .unload_model_response import UnloadModelResponse

__all__ = ["FutureRetrieveResponse"]

FutureRetrieveResponse: TypeAlias = Union[
    TryAgainResponse,
    ForwardBackwardOutput,
    OptimStepResponse,
    PublishToSamplerResponse,
    SaveWeightsResponse,
    LoadWeightsResponse,
    SaveWeightsForSamplerResponse,
    CreateModelResponse,
    CreateRuntimeResponse,
    InitTaskEnvResponse,
    CloseRuntimeResponse,
    SampleResponse,
    UnloadModelResponse,
    RequestFailedResponse,
]
