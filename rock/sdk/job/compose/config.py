"""ComposeJobConfig — Docker Compose multi-container job configuration.

Extends BashJobConfig with a ``compose`` block that describes the inner
DinD container orchestration (main + sidecars + init containers).

Type detection signal: ``"compose" in yaml_data``
"""

from __future__ import annotations

import re
from datetime import datetime

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rock.logger import init_logger
from rock.sdk.job.config import BashJobConfig

logger = init_logger(__name__)

# Regex for valid container names (used as docker --network-alias)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


# ── Sub-models: inner container specs ────────────────────────────────────────


class ResourceSpec(BaseModel):
    """Single inner-container resource declaration.

    request / limit dual-value (aligns with K8s requests/limits semantics).
    In single-host docker mode:
      cpus         → --cpus (used as hard limit when cpu_limit absent)
      cpu_limit    → --cpus (hard upper bound, takes priority)
      memory       → --memory-reservation (soft limit)
      memory_limit → --memory (hard upper bound)
    Setting only cpus/memory is the common case (treated as hard limit).
    """

    model_config = ConfigDict(extra="forbid")

    cpus: float | None = None
    memory: str | None = None
    cpu_limit: float | None = None
    memory_limit: str | None = None


class VolumeMount(BaseModel):
    """Container volume mount.

    By default ``name`` refers to the shared named volume used for cross-container
    (init → main) data passing, mounted at ``mount_path``.

    When ``host_path`` is set, the mount instead bind-mounts a real path from the
    OUTER sandbox into the container at ``mount_path`` — e.g. to expose the outer
    docker socket (``host_path: /var/run/docker.sock``) so the container reuses the
    outer dockerd instead of starting its own (avoids the 3rd DinD layer).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    mount_path: str
    main_mount_path: str | None = None
    host_path: str | None = None
    read_only: bool = False


class SecretEnvEntry(BaseModel):
    """Source declaration for a single secret environment variable (K8s Secret style)."""

    model_config = ConfigDict(extra="forbid")

    secret_name: str
    secret_key: str


class OssDep(BaseModel):
    """A single dependency to download from OSS before running."""

    model_config = ConfigDict(extra="forbid")

    key: str
    target_path: str
    extract: bool = False


class HealthSpec(BaseModel):
    """Sidecar readiness probe (optional)."""

    model_config = ConfigDict(extra="forbid")

    port: int
    timeout_sec: int = 60


class _ContainerBase(BaseModel):
    """Common fields for init and sidecar containers.

    Entry-point (choose at most one):
      - script       Inline shell (runner writes to sandbox then runs with ``bash``)
      - script_path  Path inside sandbox (``bash <path>``)
      - command/args Override image ENTRYPOINT (not via bash), e.g. for running
                     stock images like dockerd: command=["dockerd"], args=["--tls=false"]
    All three absent → use image's own ENTRYPOINT/CMD.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    image: str
    script: str | None = None
    script_path: str | None = None
    command: list[str] | None = None
    args: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: dict[str, SecretEnvEntry] = Field(default_factory=dict)
    resources: ResourceSpec | None = None
    privileged: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"Container name '{v}' is invalid. Must match ^[a-z0-9][a-z0-9-]*$ (used as docker --network-alias)."
            )
        return v

    @model_validator(mode="after")
    def _entrypoint_exclusive(self) -> _ContainerBase:
        modes = [bool(self.script), bool(self.script_path), bool(self.command)]
        if sum(modes) > 1:
            raise ValueError(
                f"container '{self.name}': script / script_path / command are mutually exclusive — use at most one"
            )
        if self.args and not self.command:
            raise ValueError(f"container '{self.name}': args must be used together with command")
        return self


class InitContainerSpec(_ContainerBase):
    """Init container: runs serially before the main container starts."""

    volume_mounts: list[VolumeMount] = Field(default_factory=list)


class SidecarSpec(_ContainerBase):
    """Sidecar container: runs in parallel with main; name becomes docker network-alias."""

    health: HealthSpec | None = None
    volume_mounts: list[VolumeMount] = Field(default_factory=list)


class MainContainerSpec(BaseModel):
    """Main container spec. Entry-point script is provided by ComposeJobConfig top-level script/script_path."""

    model_config = ConfigDict(extra="forbid")

    image: str
    resources: ResourceSpec | None = None
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: dict[str, SecretEnvEntry] = Field(default_factory=dict)
    oss_deps: list[OssDep] = Field(default_factory=list)
    volume_mounts: list[VolumeMount] = Field(default_factory=list)
    privileged: bool = False


class ComposeSpec(BaseModel):
    """Top-level compose block: inner docker orchestration inside DinD."""

    model_config = ConfigDict(extra="forbid")

    main: MainContainerSpec
    init_containers: list[InitContainerSpec] = Field(default_factory=list)
    sidecars: list[SidecarSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> ComposeSpec:
        names = [c.name for c in self.init_containers] + [s.name for s in self.sidecars]
        if len(names) != len(set(names)):
            raise ValueError("compose: init_containers / sidecars names must be globally unique")
        return self


# ── ComposeJobConfig ──────────────────────────────────────────────────────────


class ComposeJobConfig(BashJobConfig):
    """Docker Compose multi-container Job configuration.

    Inherits from BashJobConfig:
      - script / script_path at the top level describe the main container entry-point
      - environment describes the outer DinD sandbox

    Adds a top-level ``compose`` block describing the inner container orchestration.
    Type-detection signal: presence of ``compose`` key in YAML data.
    """

    model_config = ConfigDict(extra="forbid")

    job_name: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d__%H-%M-%S"))

    compose: ComposeSpec  # required; its presence identifies ComposeJobConfig

    @model_validator(mode="after")
    def _proxy_conflict_check(self) -> ComposeJobConfig:
        """Disallow environment.proxy together with a sidecar named 'proxy' (double-proxy)."""
        if self.environment.proxy and self.environment.proxy.enabled:
            if any(s.name == "proxy" for s in self.compose.sidecars):
                raise ValueError(
                    "environment.proxy and a sidecar named 'proxy' cannot both be enabled. "
                    "Choose one: use the proxy sidecar container, or use the sandbox model-service."
                )
        return self

    @model_validator(mode="after")
    def _resource_budget_check(self) -> ComposeJobConfig:
        """Warn (not fail) when inner container resources exceed the outer sandbox budget."""
        try:
            self._check_resource_budget()
        except Exception:
            pass  # never fail validation due to budget check errors
        return self

    def _check_resource_budget(self) -> None:
        """Internal helper: accumulate inner cpus/memory and warn if they exceed outer sandbox."""

        outer_cpus: float | None = getattr(self.environment, "cpus", None)
        outer_memory_str: str | None = getattr(self.environment, "memory", None)

        def parse_memory_gb(s: str | None) -> float | None:
            if s is None:
                return None
            s = s.strip().lower()
            if s.endswith("gi"):
                return float(s[:-2])
            if s.endswith("g"):
                return float(s[:-1])
            if s.endswith("mi"):
                return float(s[:-2]) / 1024
            if s.endswith("m"):
                return float(s[:-1]) / 1024
            return None

        def container_cpus(r: ResourceSpec | None) -> float:
            if r is None:
                return 0.0
            return r.cpu_limit or r.cpus or 0.0

        def container_mem(r: ResourceSpec | None) -> float:
            if r is None:
                return 0.0
            return parse_memory_gb(r.memory_limit) or parse_memory_gb(r.memory) or 0.0

        all_specs: list[ResourceSpec | None] = (
            [self.compose.main.resources]
            + [c.resources for c in self.compose.init_containers]
            + [s.resources for s in self.compose.sidecars]
        )

        total_cpus = sum(container_cpus(r) for r in all_specs)
        total_mem = sum(container_mem(r) for r in all_specs)

        if outer_cpus is not None and total_cpus > 0 and total_cpus > outer_cpus:
            logger.warning(
                "ComposeJobConfig resource budget: inner containers total cpus=%.1f "
                "exceeds outer sandbox cpus=%.1f — may cause OOM or throttling.",
                total_cpus,
                outer_cpus,
            )

        if outer_memory_str is not None and total_mem > 0:
            outer_mem = parse_memory_gb(outer_memory_str)
            if outer_mem is not None and total_mem > outer_mem:
                logger.warning(
                    "ComposeJobConfig resource budget: inner containers total memory=%.1fGi "
                    "exceeds outer sandbox memory=%.1fGi — may cause OOM.",
                    total_mem,
                    outer_mem,
                )

    @classmethod
    def from_yaml(cls, path: str) -> ComposeJobConfig:
        """Load a ComposeJobConfig from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)
