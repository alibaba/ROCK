# Derived from https://github.com/thinking-machines-lab/tinker (Apache-2.0).
# Modified in this Tinker fork; see rock-tinker/UPSTREAM.md and NOTICE.
from .._models import StrictBase
from .request_id import RequestID

__all__ = ["FutureRetrieveRequest"]


class FutureRetrieveRequest(StrictBase):
    request_id: RequestID | None = None
    """The ID of the request to retrieve."""

    future_id: RequestID | None = None
    """Alias used by Tinker backend runtime APIs; maps to the same future record."""

    allow_metadata_only: bool = False
    """When True, the server may return only response metadata (status and size)
    instead of the full payload if the response exceeds the server's inline size limit."""
