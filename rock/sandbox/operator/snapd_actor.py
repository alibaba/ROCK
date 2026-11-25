"""Ray placement and resource reservations for worker snapd services."""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

import ray


@ray.remote(num_cpus=1, max_retries=0)
def select_snapd_worker() -> str:
    """Let Ray select a worker; Admin performs the subsequent snapd request."""
    return ray.util.get_node_ip_address()


@ray.remote(max_restarts=0)
class SnapdSandboxActor:
    """Hold Ray resources and expose the worker selected for one sandbox."""

    def __init__(
        self,
        sandbox_id: str,
        sandbox_namespace: str,
        resource_spec: dict[str, Any],
        *,
        actor_generation: str | None = None,
    ) -> None:
        if not sandbox_id or not sandbox_namespace:
            raise ValueError("sandbox ID and namespace must not be empty")
        self._sandbox_id = sandbox_id
        self._sandbox_namespace = sandbox_namespace
        self._resource_spec = deepcopy(resource_spec)
        self._actor_generation = actor_generation or uuid.uuid4().hex
        self._placement = {
            "ray_node_id": str(ray.get_runtime_context().get_node_id()),
            "host_ip": ray.util.get_node_ip_address(),
        }

    def placement(self) -> dict[str, str]:
        return dict(self._placement)

    def identity(self) -> dict[str, Any]:
        return {
            "sandbox_id": self._sandbox_id,
            "sandbox_namespace": self._sandbox_namespace,
            "actor_generation": self._actor_generation,
            "resource_spec": deepcopy(self._resource_spec),
        }
