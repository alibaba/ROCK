"""General-purpose environment configuration.

EnvironmentConfig extends SandboxConfig with common environment-level fields
(uploads, environment variables, auto-stop).
"""

from __future__ import annotations

from pydantic import Field

from rock.sdk.sandbox.config import SandboxConfig


class EnvironmentConfig(SandboxConfig):
    """General environment config — sandbox base fields + environment-level fields."""

    uploads: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Files/dirs to upload before running: [(local_path, sandbox_path), ...]. "
        "Automatically detects file vs directory and uses the appropriate upload method.",
    )
    auto_stop: bool = False
    env: dict[str, str] = Field(default_factory=dict)
