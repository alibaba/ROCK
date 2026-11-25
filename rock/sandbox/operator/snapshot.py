"""Nominal snapshot capability implemented explicitly by snapshot operators."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rock.sandbox.operator.snapd import ForkOperationResult, SnapshotOperationResult
    from rock.sandbox.snapshot_types import SnapshotSource


class AbstractSnapshotOperator(ABC):
    """Optional snapshot lifecycle kept separate from ``AbstractOperator``.

    This is deliberately a nominal ABC. A backend only advertises snapshot
    support by inheriting this class and implementing every operation; merely
    defining methods with matching names is not sufficient.
    """

    @abstractmethod
    async def resolve_base_image(self, request: Any) -> SnapshotOperationResult: ...

    @abstractmethod
    async def create_base_image(
        self,
        request: Any,
        *,
        source_worker_ip: str,
    ) -> SnapshotOperationResult: ...

    async def resolve_source(self, source: SnapshotSource) -> SnapshotSource:
        """Resolve the serving runtime identity before creating a cut attempt."""

        return source

    @abstractmethod
    async def capture_snapshot(
        self,
        source: SnapshotSource,
        request: Any,
    ) -> SnapshotOperationResult: ...

    @abstractmethod
    async def fork_snapshot(
        self,
        source: SnapshotSource,
        request: Any,
    ) -> ForkOperationResult: ...

    @abstractmethod
    async def get_snapshot_status(
        self,
        snapshot_id: str,
        *,
        source_worker_ip: str,
    ) -> SnapshotOperationResult: ...

    @abstractmethod
    async def finalize_published_snapshot(
        self,
        snapshot_id: str,
        request: Any,
        *,
        source_worker_ip: str,
    ) -> SnapshotOperationResult: ...
