"""Unified routing context: signal carrier for routing decisions.

Decouples Router from request/header DTOs so it can be built from any code
path that knows the deployment intent (start path, future warmup paths, etc.).
Adding a new routing dimension only requires exposing a field here and
registering a matcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rock.admin.proto.request import ClusterInfo, UserInfo
    from rock.deployments.config import DeploymentConfig


@dataclass(frozen=True)
class RouteContext:
    """Frozen snapshot of all signals that may participate in routing.

    Frozen because routing is referentially transparent: same context →
    same operator. Any mutation indicates a logic bug.
    """

    image: str
    image_os: str
    accelerator_type: str | None
    num_gpus: float | None
    use_kata_runtime: bool
    namespace: str
    cluster: str
    user_id: str
    experiment_id: str

    @classmethod
    def from_deployment(
        cls,
        config: "DeploymentConfig",
        user_info: "UserInfo | None" = None,
        cluster_info: "ClusterInfo | None" = None,
    ) -> "RouteContext":
        """Construct from the values reaching ``SandboxManager.start_async``.

        Tolerates DeploymentConfig variants that lack docker-specific fields
        (e.g. ``LocalDeploymentConfig`` has no ``image``) by reading via
        ``getattr``. Header defaults ("default") are propagated as-is —
        matchers should treat "default" as a real value and not as ``None``.
        """
        user_info = user_info or {}
        cluster_info = cluster_info or {}
        return cls(
            image=getattr(config, "image", "") or "",
            image_os=getattr(config, "image_os", "") or "",
            accelerator_type=getattr(config, "accelerator_type", None),
            num_gpus=getattr(config, "num_gpus", None),
            use_kata_runtime=bool(getattr(config, "use_kata_runtime", False)),
            namespace=user_info.get("namespace", "default") or "default",
            cluster=cluster_info.get("cluster_name", "default") or "default",
            user_id=user_info.get("user_id", "default") or "default",
            experiment_id=user_info.get("experiment_id", "default") or "default",
        )
