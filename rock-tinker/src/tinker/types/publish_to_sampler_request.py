from typing import Optional

from typing_extensions import Literal

from .._compat import PYDANTIC_V2, ConfigDict
from .._models import StrictBase
from .model_id import ModelID

__all__ = ["PublishToSamplerRequest"]


class PublishToSamplerRequest(StrictBase):
    model_id: ModelID

    path: Optional[str] = None
    """Optional persistent sampler checkpoint name."""

    sampling_session_seq_id: Optional[int] = None

    seq_id: Optional[int] = None

    ttl_seconds: Optional[int] = None
    """TTL in seconds for this publish artifact (None = backend default)."""

    type: Literal["publish_to_sampler"] = "publish_to_sampler"

    if PYDANTIC_V2:
        model_config = ConfigDict(protected_namespaces=tuple())
