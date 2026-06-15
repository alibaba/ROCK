"""POJOs for the `Image` declaration: registry coords, builder config, build spec."""

from __future__ import annotations

from pydantic import BaseModel, Field

from rock.sdk.sandbox.config import SandboxConfig


class ImageRegistry(BaseModel):
    """Registry push target + credentials.

    Fields are populated from the admin ``/acr_config`` endpoint at
    ``Sandbox.start()`` time for any values the caller did not set explicitly.
    ``repository`` falls back to ``SandboxConfig.user_id``.
    """

    url: str | None = None
    namespace: str | None = None
    repository: str | None = None
    username: str | None = None
    password: str | None = None


class BuilderConfig(SandboxConfig):
    """SandboxConfig specialized for the DinD builder sandbox: `image` narrowed
    to `str` (a builder cannot itself trigger a nested `Image` build), and
    timeouts widened for the heavier build workload.
    """

    # Fields callers might want to inherit from their user SandboxConfig when
    # they didn't explicitly set them on builder_config — see inherit_from().
    _INHERITABLE_FIELDS = ("base_url", "extra_headers", "cluster", "user_id")

    image: str | None = None
    startup_timeout: float = 600.0
    auto_clear_seconds: int = 60 * 30

    def inherit_from(self, sandbox_config: SandboxConfig) -> BuilderConfig:
        """Return a copy with `_INHERITABLE_FIELDS` filled from `sandbox_config`
        — but only for fields the caller didn't explicitly set on `self`. So a
        BuilderConfig() picks up the user's base_url/cluster/etc., while a
        BuilderConfig(cluster="other") keeps "other".
        """
        updates = {f: getattr(sandbox_config, f) for f in self._INHERITABLE_FIELDS if f not in self.model_fields_set}
        return self.model_copy(update=updates) if updates else self


class BuildSpec(BaseModel):
    """Pre-resolved build request — what `ImageBuilder` consumes.

    Produced by `Image.to_build_spec()`; keeps ImageBuilder decoupled from Image.
    """

    image: str  # full tag: registry/namespace/repository:content_hash
    content_hash: str
    dockerfile_path: str  # file (single-Dockerfile mode) or dir (context mode)
    build_args: dict[str, str] = Field(default_factory=dict)
    registry_username: str | None = None
    registry_password: str | None = None
    force_build: bool = False
