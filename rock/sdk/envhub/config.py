"""General-purpose environment configuration.

EnvironmentConfig extends SandboxConfig with common environment-level fields
(uploads, environment variables).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from rock.sdk.sandbox.config import SandboxConfig


class OssMirrorConfig(BaseModel):
    """OSS artifact mirror configuration.

    ``namespace`` / ``experiment_id`` are synced from ``JobConfig``
    top-level fields via model validators (HarborJobConfig) or
    from sandbox properties at setup time (BashTrial).
    """

    enabled: bool = False
    oss_bucket: str | None = None
    namespace: str | None = None
    experiment_id: str | None = None
    oss_access_key_id: str | None = None
    oss_access_key_secret: str | None = None
    oss_region: str | None = None
    oss_endpoint: str | None = None


class ProxyConfig(BaseModel):
    """In-sandbox OpenAI request record/replay proxy config.

    Reuses ``rock.sdk.sandbox.model_service.ModelService`` (which uses
    ``PythonRuntimeEnv`` to install a self-contained Python), so the
    base image does not need Python preinstalled.
    """

    enabled: bool = False

    recording_file: str | None = None
    """Recording mode: absolute path inside the sandbox where the proxy appends
    the jsonl. When None (default), model-service picks its own path
    (``$ROCK_MODEL_SERVICE_DATA_DIR/LLMTraj.jsonl``)."""

    replay_file: str | None = None
    """Replay mode: local path. SDK uploads it to the sandbox and the proxy reads
    the in-sandbox copy. Mutually exclusive with recording_file."""

    host: str = "0.0.0.0"
    """Proxy listen address. 0.0.0.0 lets nested docker containers reach it via
    the docker0 gateway."""

    port: int = 28080
    """Avoid common ports like 8080 to reduce collision risk with other services
    in the sandbox."""

    model_service_package: str = "rl-rock[model-service]"
    """Package spec for installing the proxy. Must include the ``[model-service]``
    extra. Use PEP 508 syntax to pin a specific wheel, e.g.
    ``"rl-rock[model-service] @ http://.../rl_rock-X.Y.Z-py3-none-any.whl"``.
    """

    @model_validator(mode="after")
    def _record_replay_mutually_exclusive(self):
        if self.recording_file and self.replay_file:
            raise ValueError(
                "ProxyConfig.recording_file and replay_file are mutually exclusive — "
                "set one (recording mode) or the other (replay mode), not both."
            )
        return self


class EnvironmentConfig(SandboxConfig):
    """General environment config — sandbox base fields + environment-level fields."""

    uploads: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Files/dirs to upload before running: [(local_path, sandbox_path), ...]. "
        "Automatically detects file vs directory and uses the appropriate upload method.",
    )
    env: dict[str, str] = Field(default_factory=dict)
    oss_mirror: OssMirrorConfig | None = None
    proxy: ProxyConfig | None = None
    """In-sandbox model-service proxy for OpenAI request record/replay.
    None (default) means no proxy is started."""
