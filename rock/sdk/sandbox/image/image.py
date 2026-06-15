from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from rock.sdk.sandbox.image.config import BuilderConfig, BuildSpec, ImageRegistry


class Image(BaseModel):
    """Image declaration. Construct via `Image.from_dockerfile()`; for a
    pre-built image, pass the tag string directly to `SandboxConfig.image`.
    """

    dockerfile_path: str | None = None
    registry: ImageRegistry = Field(default_factory=ImageRegistry)
    force_build: bool = False
    build_args: dict[str, str] = Field(default_factory=dict)
    # Sandbox.start() fills networking fields here from the user's SandboxConfig
    # when they aren't explicitly set, so the builder hits the same admin/cluster.
    builder_config: BuilderConfig = Field(default_factory=BuilderConfig)

    _full_name: str | None = PrivateAttr(default=None)

    @staticmethod
    def from_dockerfile(
        path: str | Path,
        *,
        registry: ImageRegistry | None = None,
        force_build: bool = False,
        build_args: dict[str, str] | None = None,
        builder_config: BuilderConfig | None = None,
    ) -> Image:
        """Create from a local Dockerfile.

        `path` is either a build-context directory (must contain `Dockerfile`)
        or a path to a single self-contained Dockerfile file (in file mode,
        sibling files are NOT part of the context).

        Resulting image tag: `{registry.url}/{registry.namespace}/{registry.repository}:{sha256}`.
        Unset registry/builder fields are populated from the admin ``/acr_config``
        endpoint at ``Sandbox.start()`` time; ``repository`` falls back to
        ``SandboxConfig.user_id``.
        """
        return Image(
            dockerfile_path=str(Path(path).resolve()),
            registry=registry or ImageRegistry(),
            force_build=force_build,
            build_args=build_args or {},
            builder_config=builder_config or BuilderConfig(),
        )

    @model_validator(mode="after")
    def _validate(self) -> Image:
        if self.dockerfile_path is None:
            raise ValueError("Image must have 'dockerfile_path'")
        p = Path(self.dockerfile_path)
        if p.is_dir():
            if not (p / "Dockerfile").exists():
                raise ValueError(f"No Dockerfile found in: {self.dockerfile_path}")
        elif not p.is_file():
            raise ValueError(f"dockerfile_path is neither a file nor a directory: {self.dockerfile_path}")
        return self

    def content_hash(self) -> str:
        """SHA-256 (64 hex) of the build context.

        Directory mode: walks all files (skipping .git). File mode: hashes
        only the Dockerfile file itself.
        """
        h = hashlib.sha256()
        p = Path(self.dockerfile_path)
        if p.is_file():
            h.update(b"Dockerfile")
            h.update(p.read_bytes())
        else:
            for f in sorted(p.rglob("*")):
                if f.is_file() and ".git" not in f.parts:
                    h.update(str(f.relative_to(p)).encode())
                    h.update(f.read_bytes())
        return h.hexdigest()

    @property
    def full_name(self) -> str:
        """`{registry.url}/{registry.namespace}/{registry.repository}:{tag}`,
        cached on first access. Raises if any segment is unresolved.
        """
        if self._full_name is None:
            r = self.registry
            if not (r.url and r.namespace and r.repository):
                missing = [
                    k
                    for k, v in [
                        ("registry.url", r.url),
                        ("registry.namespace", r.namespace),
                        ("registry.repository", r.repository),
                    ]
                    if not v
                ]
                raise ValueError(f"Cannot resolve image name, missing: {missing}")
            self._full_name = f"{r.url.rstrip('/')}/{r.namespace}/{r.repository}:{self.content_hash()}"
        return self._full_name

    def to_build_spec(self) -> BuildSpec:
        """Project this Image into the BuildSpec consumed by ImageBuilder."""
        return BuildSpec(
            image=self.full_name,
            content_hash=self.content_hash(),
            dockerfile_path=self.dockerfile_path,
            build_args=self.build_args,
            registry_username=self.registry.username,
            registry_password=self.registry.password,
            force_build=self.force_build,
        )

