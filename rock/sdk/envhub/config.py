"""General-purpose environment configuration.

EnvironmentConfig extends SandboxConfig with common environment-level fields
(uploads, environment variables).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

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


class EnvironmentConfig(SandboxConfig):
    """General environment config — sandbox base fields + environment-level fields."""

    uploads: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Files/dirs to upload before running: [(local_path, sandbox_path), ...]. "
        "Automatically detects file vs directory and uses the appropriate upload method.",
    )
    env: dict[str, str] = Field(default_factory=dict)
    oss_mirror: OssMirrorConfig | None = None
