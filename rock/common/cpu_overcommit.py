import math

from rock.common.constants import CPU_OVERCOMMIT_ALLOWED_KEYS_KEY, CPU_OVERCOMMIT_HEADROOM_KEY
from rock.deployments.config import DockerDeploymentConfig


async def apply_cpu_overcommit(
    config: DockerDeploymentConfig, nacos_provider, rock_authorization: str | None = None
) -> None:
    """Derive limit_cpus from cpus + Nacos headroom when not explicitly set.

    Formula: limit_cpus = min(2 * cpus, cpus + headroom)
    - SDK-supplied limit_cpus always wins (function is a no-op in that case).
    - Grayscale gate driven by Nacos list `cpu_overcommit_allowed_keys`:
      * key absent from Nacos -> gate is open for every caller (full rollout).
      * key present as a list  -> only `rock_authorization` values in the list pass.
      * key present but not a list (misconfigured) -> gate closed.
    - headroom is read from Nacos key `cpu_overcommit_headroom` (default 0).
    - headroom <= 0 keeps limit_cpus = None (docker run gets no --cpus flag).
    """
    if config.limit_cpus is not None:
        return

    if nacos_provider is None:
        return

    nacos_config = await nacos_provider.get_config() or {}
    allowed_keys = nacos_config.get(CPU_OVERCOMMIT_ALLOWED_KEYS_KEY)
    if allowed_keys is not None and (not isinstance(allowed_keys, list) or rock_authorization not in allowed_keys):
        return

    raw = nacos_config.get(CPU_OVERCOMMIT_HEADROOM_KEY)
    try:
        headroom = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        headroom = 0.0

    # Reject NaN / inf so a fat-fingered Nacos edit can't break sandbox startup
    if not math.isfinite(headroom) or headroom <= 0:
        return

    config.limit_cpus = min(2 * config.cpus, config.cpus + headroom)
