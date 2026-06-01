from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, Field, model_serializer, model_validator


class Image(BaseModel):
    """镜像声明，不直接构造，通过静态工厂方法创建。

    示例：
        Image.base("python:3.11")
        Image.from_dockerfile("/path/to/env_dir")
        Image.from_dockerfile(
            "/path/to/env_dir",
            registry_url="reg.io",
            namespace="rock",
            repository="my-env",
            registry_username="user",
            registry_password="pass",
        )
    """

    # ── base() 路径 ──
    image_name: str | None = None  # 仅 Image.base() 使用

    # ── from_dockerfile() 路径，4 段拼接 ──
    dockerfile_path: str | None = None
    registry_url: str | None = None  # 默认 env_vars.ROCK_IMAGE_REGISTRY
    namespace: str | None = None  # 默认 env_vars.ROCK_IMAGE_NAMESPACE ("rock")
    repository: str | None = None  # 默认 SandboxConfig.user_id（Sandbox.start() 注入）
    # tag = content_hash()（完整 64 hex SHA-256），不暴露字段

    # ── 通用 ──
    force_build: bool = False
    build_args: dict[str, str] = Field(default_factory=dict)
    registry_username: str | None = None
    registry_password: str | None = None

    @staticmethod
    def base(image: str) -> Image:
        """从已有镜像创建。等价于直接使用字符串。"""
        return Image(image_name=image)

    @staticmethod
    def from_dockerfile(
        path: str | Path,
        *,
        registry_url: str | None = None,
        namespace: str | None = None,
        repository: str | None = None,
        registry_username: str | None = None,
        registry_password: str | None = None,
        force_build: bool = False,
        build_args: dict[str, str] | None = None,
    ) -> Image:
        """从包含 Dockerfile 的本地目录创建。

        镜像名按 4 段拼接：`{registry_url}/{namespace}/{repository}:{tag}`，
        其中 tag = build context 的完整 SHA-256 (64 hex)。

        Args:
            path: 本地目录，包含 Dockerfile 和构建上下文文件。
            registry_url: registry host。不传则使用 ROCK_IMAGE_REGISTRY。
            namespace: 命名空间。不传则使用 ROCK_IMAGE_NAMESPACE（默认 "rock"）。
            repository: 仓库名。不传则在 Sandbox.start() 时使用 SandboxConfig.user_id
                （都缺失则退化为 "default"）。
            registry_username: 镜像仓库用户名。不传则使用 ROCK_IMAGE_REGISTRY_USERNAME。
            registry_password: 镜像仓库密码。不传则使用 ROCK_IMAGE_REGISTRY_PASSWORD。
            force_build: 强制重新构建，即使镜像已存在。
            build_args: Docker build 参数（--build-arg）。
        """
        return Image(
            dockerfile_path=str(Path(path).resolve()),
            registry_url=registry_url,
            namespace=namespace,
            repository=repository,
            registry_username=registry_username,
            registry_password=registry_password,
            force_build=force_build,
            build_args=build_args or {},
        )

    @model_validator(mode="after")
    def _validate(self) -> Image:
        if self.image_name is None and self.dockerfile_path is None:
            raise ValueError("Image must have either 'image_name' or 'dockerfile_path'")
        if self.dockerfile_path is not None:
            p = Path(self.dockerfile_path)
            if not p.is_dir():
                raise ValueError(f"dockerfile_path is not a directory: {self.dockerfile_path}")
            if not (p / "Dockerfile").exists():
                raise ValueError(f"No Dockerfile found in: {self.dockerfile_path}")
            if self.registry_username is None or self.registry_password is None:
                from rock import env_vars

                if self.registry_username is None:
                    self.registry_username = env_vars.ROCK_IMAGE_REGISTRY_USERNAME
                if self.registry_password is None:
                    self.registry_password = env_vars.ROCK_IMAGE_REGISTRY_PASSWORD
        return self

    @property
    def needs_build(self) -> bool:
        return self.dockerfile_path is not None

    def content_hash(self) -> str:
        """计算 dockerfile_path 目录的内容哈希（SHA-256, 64 hex）。"""
        if not self.dockerfile_path:
            return ""
        h = hashlib.sha256()
        env_dir = Path(self.dockerfile_path)
        for f in sorted(env_dir.rglob("*")):
            if f.is_file() and ".git" not in f.parts:
                h.update(str(f.relative_to(env_dir)).encode())
                h.update(f.read_bytes())
        return h.hexdigest()

    def _resolve_full_name(self) -> str:
        """拼接 registry_url/namespace/repository:tag。
        由 Sandbox.start() 在注入 repository 之后调用。
        """
        from rock import env_vars

        registry_url = self.registry_url or env_vars.ROCK_IMAGE_REGISTRY
        namespace = self.namespace or env_vars.ROCK_IMAGE_NAMESPACE
        repository = self.repository
        if not (registry_url and namespace and repository):
            missing = [
                k
                for k, v in [
                    ("registry_url", registry_url),
                    ("namespace", namespace),
                    ("repository", repository),
                ]
                if not v
            ]
            raise ValueError(f"Cannot resolve image name, missing: {missing}")
        tag = self.content_hash()
        return f"{registry_url.rstrip('/')}/{namespace}/{repository}:{tag}"

    async def build(
        self,
        *,
        base_url: str,
        cluster: str,
        extra_headers: dict[str, str] | None = None,
    ) -> str:
        """将 Image 构建为镜像 tag 字符串。

        对于 base image 直接返回 image_name。
        对于 dockerfile image，启动 builder sandbox 完成 DinD 构建和推送。
        """
        if not self.needs_build:
            return self.image_name

        from rock.sdk.sandbox.image_builder import ImageBuilder

        builder = ImageBuilder(
            base_url=base_url,
            cluster=cluster,
            extra_headers=extra_headers,
        )
        return await builder.build(self)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        if self.image_name is not None:
            return self.image_name
        return handler(self)
