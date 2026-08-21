"""RemoteProvider Protocol — abstraction for remote sandbox platform providers.

The Protocol decouples RemoteOperator from any specific platform SDK.
The first (and currently only) implementation is ``SandboxNextProvider``,
which talks to the SandboxNext Gateway REST API.

See docs/proposals/remote-operator.md for the full design.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.deployments.config import DockerDeploymentConfig


@runtime_checkable
class RemoteProvider(Protocol):
    """Protocol that every remote platform provider must implement.

    The operator delegates lifecycle calls to the provider and handles
    Redis metadata merging itself, so the provider stays pure (no Redis).
    """

    # --- Lifecycle ---

    async def submit(self, config: DockerDeploymentConfig, user_info: dict) -> SandboxInfo:
        """Create a sandbox on the remote platform.

        Returns a SandboxInfo with at least sandbox_id, host_ip, port_mapping,
        auth_token, and extended_params (including the platform-assigned ID).
        """
        ...  # pragma: no cover

    async def get_status(self, remote_sandbox_id: str) -> SandboxInfo | None:
        """Query the remote platform for current sandbox status.

        Returns None when the sandbox no longer exists (404).
        """
        ...  # pragma: no cover

    async def stop(self, remote_sandbox_id: str) -> bool:
        """Pause the sandbox. May fall back to delete if pause is unsupported."""
        ...  # pragma: no cover

    async def delete(self, remote_sandbox_id: str) -> bool:
        """Delete the sandbox. Returns True on success or already-gone (404)."""
        ...  # pragma: no cover

    # --- Template API (optional; raise NotImplementedError if unsupported) ---

    async def create_template(self, spec: Any) -> dict:
        """Create or reuse a template. 409 → idempotent GET fallback."""
        ...  # pragma: no cover

    async def get_template_status(self, template_id: str) -> dict | None:
        """Get template status. 404 → None."""
        ...  # pragma: no cover

    async def delete_template(self, template_id: str) -> bool:
        """Delete template. 404 → True (already gone)."""
        ...  # pragma: no cover
