"""ROCK-admin client contracts for the node-local rock-snapd service."""

from .client import SnapdAPIError, SnapdClient, SnapdProtocolError, SnapdResultUnknown
from .protocol import (
    CreateBaseImageRequest,
    ForkSandboxRequest,
    SnapdReply,
    SnapshotSandboxRequest,
    StartSandboxFromSnapshotRequest,
)

__all__ = [
    "CreateBaseImageRequest",
    "ForkSandboxRequest",
    "SnapshotSandboxRequest",
    "SnapdAPIError",
    "SnapdClient",
    "SnapdProtocolError",
    "SnapdReply",
    "SnapdResultUnknown",
    "StartSandboxFromSnapshotRequest",
]
