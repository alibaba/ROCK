from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, Field, model_serializer, model_validator


class Image(BaseModel):
    """镜像声明，不直接构造，通过静态工厂方法创建。

    示例：
        Image.base("python:3.11")
        Image.from_dockerfile("/path/to/env_dir")
        Image.from_dockerfile("/path/to/env_dir", image_name="reg.io/my-img:v1",
                              registry_username="user", registry_password="pass")
    """

    # TODO: 
    ## image_name的方式需要修改下
        # registry_url: 由用户传入或者使用系统默认的
        # namespace: 由用户传入或者使用系统默认的
        # repository: 使用user_id
        # tag: 使用content_hash，先不允许用户传入，
            #  需要调研下，使用content_hash是否会有冲突概率，也即是否可以认为当前方案是完备的
    image_name: str | None = None
    dockerfile_path: str | None = None
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
        image_name: str | None = None,
        registry_username: str | None = None,
        registry_password: str | None = None,
        force_build: bool = False,
        build_args: dict[str, str] | None = None,
    ) -> Image:
        """从包含 Dockerfile 的本地目录创建。

        Args:
            path: 本地目录，包含 Dockerfile 和构建上下文文件。
            image_name: 目标镜像全名。不传则自动生成：
                {ROCK_IMAGE_REGISTRY}:{content_hash[:20]}
            registry_username: 镜像仓库用户名。不传则使用 ROCK_IMAGE_REGISTRY_USERNAME。
            registry_password: 镜像仓库密码。不传则使用 ROCK_IMAGE_REGISTRY_PASSWORD。
            force_build: 强制重新构建，即使镜像已存在。
            build_args: Docker build 参数（--build-arg）。
        """
        return Image(
            dockerfile_path=str(Path(path).resolve()),
            image_name=image_name,
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
            if self.image_name is None:
                from rock import env_vars

                registry = env_vars.ROCK_IMAGE_REGISTRY
                if not registry:
                    raise ValueError("image_name is required when ROCK_IMAGE_REGISTRY is not set")
                content_hash = self.content_hash()
                self.image_name = f"{registry}:{content_hash[:20]}"
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
        """计算 dockerfile_path 目录的内容哈希（SHA-256）。"""
        if not self.dockerfile_path:
            return ""
        h = hashlib.sha256()
        env_dir = Path(self.dockerfile_path)
        for f in sorted(env_dir.rglob("*")):
            if f.is_file() and ".git" not in f.parts:
                h.update(str(f.relative_to(env_dir)).encode())
                h.update(f.read_bytes())
        return h.hexdigest()

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
