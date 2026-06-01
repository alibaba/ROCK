# Start from Dockerfile — Implementation Plan

## 背景

ROCK SDK 目前只支持通过预构建镜像名（`SandboxConfig.image: str`）启动沙箱。调用方必须在 SDK 外部完成 `docker build` + `docker push`，再将镜像 tag 传入 `Sandbox.start()`。

本次实现引入 `Image` 一等类型，支持 `Image.from_dockerfile(path)` 声明式接口。调用方将 `Image` 对象赋给 `SandboxConfig.image`，SDK 在 `Sandbox.start()` 内部透明地完成 DinD 构建、推送和缓存检查，最终将 `Image` 解析为字符串 image_name 后发送给 Admin API。方案参考 Daytona（`Image.from_dockerfile(path)` → `create()`）和 Modal（`Image.from_dockerfile(path)` → `Sandbox.create(image=image)`）的设计。

关键约束：Admin API（`SandboxStartRequest.image: str`）、DB schema（`SandboxRecord.image = Column(String(512))`）不做任何修改。`Image` 类型在 HTTP 边界之前完全解析为字符串。

---

## File Changes

| 文件 | 修改类型 | 说明 |
|------|------|------|
| `rock/sdk/sandbox/image.py` | 新增 | `Image` 类 — 含 `base()` 和 `from_dockerfile()` 工厂方法，纯声明类型 |
| `rock/sdk/sandbox/image_resolver.py` | 新增 | `_ImageResolver` — DinD 构建编排：缓存检查 + docker build + push |
| `rock/sdk/sandbox/config.py` | 修改 | `SandboxConfig.image` 类型从 `str` 改为 `str \| Image`，添加 Pydantic validator |
| `rock/sdk/sandbox/client.py` | 修改 | `Sandbox.start()` 中增加 Image 解析逻辑；`__str__` 兼容 Image 类型 |
| `tests/unit/sdk/sandbox/test_image.py` | 新增 | `Image` 类单元测试 |
| `tests/unit/sdk/sandbox/test_image_resolver.py` | 新增 | `_ImageResolver` 单元测试（mock sandbox） |
| `tests/integration/sdk/sandbox/test_image_build.py` | 新增 | 端到端集成测试：from_dockerfile → start → 验证 COPY 文件 |

---

## 核心逻辑

### 变更 1：Image 类（纯声明类型）

文件：`rock/sdk/sandbox/image.py`（新增）

`Image` 是一个 Pydantic BaseModel，提供两个静态工厂方法创建实例。`Image` 仅描述"从哪里构建、目标镜像名是什么"，不包含任何构建执行逻辑。

```python
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, model_serializer, model_validator


class Image(BaseModel):
    """镜像声明，不直接构造，通过静态工厂方法创建。

    示例：
        Image.base("python:3.11")
        Image.from_dockerfile("/path/to/env_dir")  # 使用默认 ROCK_IMAGE_REGISTRY
        Image.from_dockerfile("/path/to/env_dir", image_name="reg.io/my-img:v1",
                              registry_username="user", registry_password="pass")
    """

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
            # 自动生成 image_name
            if self.image_name is None:
                from rock import env_vars

                registry = env_vars.ROCK_IMAGE_REGISTRY
                if not registry:
                    raise ValueError("image_name is required when ROCK_IMAGE_REGISTRY is not set")
                content_hash = self.content_hash()
                self.image_name = f"{registry}:{content_hash[:20]}"
            # 自动填充默认凭证
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
        """计算 dockerfile_path 目录的内容哈希（SHA-256）。
        用于检测 env_dir 内容变化，即使 image_name 不变也能触发重建。
        """
        import hashlib

        if not self.dockerfile_path:
            return ""
        h = hashlib.sha256()
        env_dir = Path(self.dockerfile_path)
        for f in sorted(env_dir.rglob("*")):
            if f.is_file() and ".git" not in f.parts:
                h.update(str(f.relative_to(env_dir)).encode())
                h.update(f.read_bytes())
        return h.hexdigest()

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        """model_dump(mode='json') 时输出 tag 字符串。
        兼容 Harbor YAML 序列化路径，同时避免凭证泄漏到序列化输出。
        """
        if self.image_name is not None:
            return self.image_name
        return handler(self)
```

### 变更 2：_ImageResolver 类（DinD 构建编排）

文件：`rock/sdk/sandbox/image_resolver.py`（新增）

`_ImageResolver` 是内部执行类，承担 DinD 构建编排。接收 `Image` 对象，使用其自身的凭证和参数完成构建流程。

```python
from __future__ import annotations

import io
import logging
import os
import shlex
import tarfile
import tempfile
from pathlib import Path

from rock.sdk.sandbox.image import Image

logger = logging.getLogger(__name__)


class _ImageResolver:
    """将 Image 声明解析为镜像 tag 字符串。

    对于 base image 直接返回 tag。
    对于 dockerfile image，启动一个 builder sandbox 完成 DinD 构建和推送。
    """

    def __init__(
        self,
        *,
        base_url: str,
        cluster: str,
        extra_headers: dict[str, str] | None = None,
        builder_image: str | None = None,
        _sandbox_factory=None,
    ):
        self._base_url = base_url
        self._cluster = cluster
        self._extra_headers = extra_headers or {}
        self._builder_image = builder_image
        self._sandbox_factory = _sandbox_factory

    async def resolve(self, image: Image) -> str:
        """解析 Image 为镜像 tag 字符串。

        对于 base image 直接返回 tag。
        对于 dockerfile image，启动一个 builder sandbox 完成：
        1. docker manifest inspect 检查镜像是否已存在
        2. 若不存在（或 force_build），执行 docker build + push
        3. 返回 image_name
        """
        if not image.needs_build:
            return image.image_name

        from rock import env_vars
        from rock.actions import CreateBashSessionRequest
        from rock.sdk.sandbox.client import Sandbox
        from rock.sdk.sandbox.config import SandboxConfig
        from rock.utils import ImageUtil

        # 默认使用 Docker 官方 DinD 镜像（与 Daytona 一致）
        builder_image = self._builder_image or env_vars.ROCK_IMAGE_BUILDER_IMAGE or "docker:28.3.3-dind"
        builder_cfg = SandboxConfig(
            image=builder_image,
            base_url=self._base_url,
            cluster=self._cluster,
            extra_headers=self._extra_headers,
            registry_username=image.registry_username,
            registry_password=image.registry_password,
            startup_timeout=600.0,
            auto_clear_seconds=60 * 30,
        )
        factory = self._sandbox_factory or Sandbox
        builder = factory(builder_cfg)
        session = "build"
        try:
            await builder.start()
            await builder.create_session(CreateBashSessionRequest(session=session))

            # ── Registry login ──
            if image.registry_username and image.registry_password:
                registry, _ = ImageUtil.parse_registry_and_others(image.image_name)
                if not registry:
                    registry = "docker.io"
                await builder.arun(
                    cmd=f"echo {shlex.quote(image.registry_password)} | docker login {shlex.quote(registry)} "
                    f"-u {shlex.quote(image.registry_username)} --password-stdin",
                    session=session,
                )

            # ── 计算 env_dir 内容哈希 ──
            content_hash = image.content_hash()

            # ── 缓存检查：docker manifest inspect + 内容哈希对比 ──
            if not image.force_build:
                check = await builder.arun(
                    cmd=f"docker manifest inspect {shlex.quote(image.image_name)} > /dev/null 2>&1"
                    f" && echo EXISTS || echo MISSING",
                    session=session,
                )
                if "EXISTS" in (check.output or ""):
                    # 镜像存在，进一步检查内容哈希是否匹配
                    inspect_cmd = (
                        f"docker pull {shlex.quote(image.image_name)} > /dev/null 2>&1 && "
                        f"docker inspect --format='{{{{index .Config.Labels \"rock.content_hash\"}}}}' {shlex.quote(image.image_name)}"
                    )
                    result = await builder.arun(cmd=inspect_cmd, session=session)
                    remote_hash = (result.output or "").strip()
                    if remote_hash == content_hash:
                        logger.info("Image %s exists and content unchanged, skipping build", image.image_name)
                        return image.image_name
                    else:
                        logger.info("Image %s exists but content changed (remote=%s, local=%s), rebuilding",
                                    image.image_name, remote_hash[:12], content_hash[:12])

            # ── 启动 dockerd ──
            await builder.arun(cmd="service docker start", session=session)

            # ── 上传构建上下文 ──
            context_path = await self._upload_context(builder, session, image)

            # ── docker build（将内容哈希写入镜像 label）──
            build_arg_flags = " ".join(
                f"--build-arg {shlex.quote(f'{k}={v}')}" for k, v in image.build_args.items()
            )
            label_flag = f"--label rock.content_hash={shlex.quote(content_hash)}"
            build_cmd = f"docker build {build_arg_flags} {label_flag} -t {shlex.quote(image.image_name)} {shlex.quote(context_path)}".strip()
            obs = await builder.arun(cmd=build_cmd, session=session, wait_timeout=600, mode="nohup")
            if obs.exit_code != 0:
                raise RuntimeError(f"docker build failed: {obs.failure_reason or obs.output}")

            # ── docker push ──
            obs = await builder.arun(cmd=f"docker push {shlex.quote(image.image_name)}", session=session, wait_timeout=300, mode="nohup")
            if obs.exit_code != 0:
                raise RuntimeError(f"docker push failed: {obs.failure_reason or obs.output}")

            logger.info("Successfully built and pushed image %s", image.image_name)
            return image.image_name
        finally:
            try:
                await builder.stop()
            except Exception:
                logger.warning("Failed to stop builder sandbox: %s", builder.sandbox_id, exc_info=True)

    async def _upload_context(self, builder, session: str, image: Image) -> str:
        """将 environment_dir 打包为 tar.gz 上传到 builder sandbox，返回解压后的远程路径。"""
        remote_tar = "/tmp/rock_env_dir.tar.gz"
        remote_ctx = "/tmp/rock_env_dir_ctx"

        env_dir = Path(image.dockerfile_path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(env_dir, arcname=".", filter=lambda ti: None if ti.name == ".git" else ti)
        tar_bytes = buf.getvalue()

        with tempfile.NamedTemporaryFile(prefix="rock_env_dir_", suffix=".tar.gz", delete=False) as f:
            f.write(tar_bytes)
            local_tar_path = f.name
        try:
            upload_resp = await builder.upload_by_path(file_path=local_tar_path, target_path=remote_tar)
            if not upload_resp.success:
                raise RuntimeError(f"Failed to upload build context: {upload_resp.message}")
        finally:
            try:
                os.remove(local_tar_path)
            except OSError:
                pass

        await builder.arun(cmd=f"mkdir -p {remote_ctx}", session=session)
        await builder.arun(cmd=f"tar -xzf {remote_tar} -C {remote_ctx}", session=session)
        return remote_ctx
```

### 变更 3：SandboxConfig.image 类型扩展

文件：`rock/sdk/sandbox/config.py`

`image` 字段类型从 `str` 改为 `str | Image`，通过 `field_validator` 保持向后兼容：

```python
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rock.sdk.sandbox.image import Image


class SandboxConfig(BaseConfig):
    image: str | Image = "python:3.11"    # 扩展类型
    # ... 其他字段不变

    @field_validator("image", mode="before")
    @classmethod
    def _coerce_image(cls, v):
        from rock.sdk.sandbox.image import Image

        if isinstance(v, (str, Image)):
            return v
        if isinstance(v, dict):
            try:
                return Image(**v)
            except Exception:
                pass
        return v
```

### 变更 4：Sandbox.start() Image 解析

文件：`rock/sdk/sandbox/client.py`，`start` 方法

在构建 POST body 之前，检测 `self.config.image` 是否为 `Image` 对象，若是则通过 `_ImageResolver` 解析为字符串 tag 并回写：

```python
async def start(self):
    # ── Image 解析 ──
    from rock.sdk.sandbox.image import Image

    if isinstance(self.config.image, Image):
        image_obj = self.config.image
        if image_obj.needs_build:
            from rock.sdk.sandbox.image_resolver import _ImageResolver

            resolver = _ImageResolver(
                base_url=self.config.base_url,
                cluster=self.config.cluster,
                extra_headers=self.config.extra_headers,
            )
            resolved_name = await resolver.resolve(image_obj)
            self.config.image = resolved_name
            # 同步凭证到 SandboxConfig，供 Admin 拉取镜像
            if image_obj.registry_username and not self.config.registry_username:
                self.config.registry_username = image_obj.registry_username
                self.config.registry_password = image_obj.registry_password
        else:
            self.config.image = image_obj.image_name
    # ── 此时 self.config.image 必定为 str ──

    url = f"{self._url}/start_async"
    # ... 原有逻辑不变
```

`__str__` 兼容处理：

```python
def __str__(self):
    from rock.sdk.sandbox.image import Image

    image_display = self.config.image
    if isinstance(image_display, Image):
        image_display = f"Image(image_name={image_display.image_name}, dockerfile={image_display.dockerfile_path})"

    return (
        f"Sandbox(sandbox_id={self._sandbox_id}, "
        f"host_name={self._host_name!r}, "
        f"host_ip={self._host_ip}, "
        f"image={image_display}, "
        f"cluster={self._cluster})"
    )
```

### 设计要点

1. **Image 位于 `rock/sdk/sandbox/image.py`**：`Image` 是 `SandboxConfig.image` 字段的类型，与 `SandboxConfig` 紧密耦合，放在同一模块下。

2. **声明与执行分离**：`Image` 是纯声明类型，仅描述"从哪里构建、目标镜像名是什么"；`_ImageResolver` 承担 DinD 构建编排，是内部实现类（前导下划线）。调用方只需构造 `Image` 对象，`Sandbox.start()` 内部自动使用 `_ImageResolver` 完成解析。两个类职责清晰：`Image` 负责验证和序列化，`_ImageResolver` 负责构建执行。

3. **resolve-and-replace**：在 `Sandbox.start()` 最顶部将 `Image` 解析为 `str` 并回写 `self.config.image`。此后所有下游代码（POST body、`__str__`、`SandboxGroup` 日志）自动看到纯字符串，无需逐一修改。

4. **Image 自包含凭证**：`Image` 自带 `registry_username` / `registry_password`，`_ImageResolver` 直接从 `Image` 对象读取凭证完成 registry login、push。解析完成后，`Sandbox.start()` 将凭证同步到 `SandboxConfig`（供 Admin 拉取镜像）。`Image` 作为一等类型，不依赖 `SandboxConfig` 即可独立完成构建。镜像仓库地址从 `image_name` 中解析（`ImageUtil.parse_registry_and_others()`）。

5. **缓存检查与构建合并**：在同一个 builder sandbox 会话中完成缓存检查和构建。缓存检查分两层：①`docker manifest inspect` 检查 image_name 是否存在于 registry；②若存在，`docker pull` + `docker inspect` 对比镜像 label 中的 `rock.content_hash` 与当前 env_dir 的内容哈希。两者都匹配才跳过构建，内容变化即使 tag 不变也会触发重建。构建时通过 `--label rock.content_hash=<hash>` 将哈希写入镜像。

6. **Admin 侧零改动**：`SandboxStartRequest.image: str`、`SandboxRecord.image = Column(String(512))` 不修改。`Image` 在 `Sandbox.start()` 的 HTTP 调用之前已解析为字符串。

7. **Pydantic 序列化兼容**：`Image` 的 `model_serializer(mode="wrap")` 在 `model_dump(mode="json")` 时输出 image_name 字符串。确保 `HarborJobConfig.to_harbor_yaml()` 和 `RockEnvironmentConfig.to_harbor_environment()` 的序列化路径不受影响。

8. **image_name 自动生成**：`from_dockerfile()` 不要求调用方传入 `image_name`。当 `image_name` 为空时，从环境变量 `ROCK_IMAGE_REGISTRY` 读取镜像名（不含 tag），拼接 content_hash 前 20 位作为 tag，生成 `{ROCK_IMAGE_REGISTRY}:{content_hash[:20]}`。content_hash 作为 tag 意味着内容变化自动产生新 tag，无需额外的 label 对比即可识别缓存。凭证同理：`registry_username` / `registry_password` 为空时自动从 `ROCK_IMAGE_REGISTRY_USERNAME` / `ROCK_IMAGE_REGISTRY_PASSWORD` 读取。

9. **Builder 镜像选择**：`_ImageResolver` 启动 builder sandbox 时默认使用 `docker:28.3.3-dind`（Docker 官方 DinD 镜像），与 Harbor Daytona 环境使用的构建镜像一致。可通过 `ROCK_IMAGE_BUILDER_IMAGE` 环境变量或 `builder_image` 参数覆盖。

---

## Validation Plan

### 测试数据

路径：`tests/integration/test_data/image_from_dockerfile/`

包含最小构建上下文：`Dockerfile`（`FROM python:3.11` + `COPY hello.txt`）和 `hello.txt` 标记文件。

### 集成测试 — `tests/integration/sdk/sandbox/test_image_build.py`

| 测试名 | 验证点 | marker |
|--------|--------|--------|
| `test_from_dockerfile_build_and_start` | `Image.from_dockerfile(path)` → `Sandbox.start()` → `cat /opt/hello.txt` 验证 COPY 文件可访问 | `@pytest.mark.need_admin` |
| `test_from_dockerfile_cache_skip` | 第二次 start 同一 Image，缓存命中跳过构建，耗时显著低于首次 | `@pytest.mark.need_admin` |
| `test_from_dockerfile_force_build` | `force_build=True` 即使镜像已存在也重新构建，验证文件内容正确 | `@pytest.mark.need_admin` |

### 回归测试

```bash
uv run pytest -m "not need_ray and not need_admin and not need_admin_and_network" --reruns 1
```

---

## Rollback

- 删除新文件 `rock/sdk/sandbox/image.py`、`rock/sdk/sandbox/image_resolver.py` 及对应测试
- 还原 `rock/sdk/sandbox/config.py`（`image: str | Image` → `image: str`，移除 validator）
- 还原 `rock/sdk/sandbox/client.py`（移除 `start()` 中的 Image 解析、`__str__` 类型检查）
- Admin 侧无变更需回滚
