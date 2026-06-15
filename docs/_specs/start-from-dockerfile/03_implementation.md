# Start from Dockerfile — Implementation Plan

## 背景

ROCK SDK 现状只接受预构建镜像 tag（`SandboxConfig.image: str`），调用方必须在 SDK 外部自己 `docker build` + `docker push`。本次引入 `Image` 一等类型，提供 `Image.from_dockerfile(path)` 声明式接口；SDK 在 `Sandbox.start()` 内透明完成 DinD 构建、推送、缓存检查，再以纯字符串 image_name 调 Admin API。

关键约束：Admin API（`SandboxStartRequest.image: str`）与 DB schema（`SandboxRecord.image = Column(String(512))`）零改动。`Image` 在 HTTP 边界之前必须解析为字符串。

参考：Daytona `Image.from_dockerfile(path) → create()`、Modal `Image.from_dockerfile(path) → Sandbox.create(image=image)`。

---

## 架构

```
用户            SDK                                      Admin
─────  ──────────────────────────────────────────────  ─────
Image.from_dockerfile(path)
  │
  ▼
SandboxConfig(image=Image)
  │
  ▼
Sandbox.start()
  │
  ├─► _resolve_image()
  │       │
  │       ├─► image.to_build_spec()  ──► BuildSpec（扁平契约，与 Image 结构解耦）
  │       │
  │       └─► ImageBuilder.build(spec)
  │              │
  │              ├─► 起 builder sandbox（DinD）
  │              ├─► docker manifest inspect  (缓存检查)
  │              ├─► docker build + label rock.content_hash
  │              └─► docker push
  │       │
  │       ▼
  │   image_name: str
  │
  └─► POST /start_async {image: "..."} ─────────────► (零改动)
```

---

## File Changes

| 文件 | 类型 | 说明 |
|------|------|------|
| `rock/sdk/sandbox/image/image.py` | 新增 | `Image` 类 — `from_dockerfile()` 工厂方法，纯声明类型；4 段拼接命名；`to_build_spec()` 投影成 `BuildSpec` |
| `rock/sdk/sandbox/image/image_builder.py` | 新增 | `ImageBuilder` — DinD 构建编排：起 builder sandbox、缓存检查、build、push。纯消费 `BuildSpec` + `BuilderConfig`，不依赖 `Image` |
| `rock/sdk/sandbox/image/config.py` | 新增 | `ImageRegistry`（推送目标 + 凭证）、`BuilderConfig`（builder sandbox 配置）、`BuildSpec`（`Image` → `ImageBuilder` 的扁平契约） |
| `rock/sdk/sandbox/image/__init__.py` | 新增 | 子包入口；触发 `SandboxConfig.model_rebuild` 解开 `str \| Image` 前向引用 |
| `rock/sdk/sandbox/config.py` | 修改 | `SandboxConfig.image` 从 `str` 扩展为 `str \| Image` |
| `rock/sdk/sandbox/client.py` | 修改 | `Sandbox.start()` 入口调 `_resolve_image()`：构造 `ImageBuilder`，把 `Image` 解析回写为 str |
| `rock/env_vars.py` | 修改 | 新增 `ROCK_IMAGE_NAMESPACE` (默认 `"rock"`)、`ROCK_IMAGE_BUILDER_IMAGE` |
| `tests/unit/sdk/sandbox/test_image.py` | 新增 | `Image` 单元测试 |
| `tests/integration/sdk/sandbox/test_image_build.py` | 新增 | from_dockerfile → start 端到端集成测试 |

---

## 设计要点

1. **声明与执行分离**：`Image` 纯声明（验证、序列化、命名拼接），`ImageBuilder` 负责执行（起 builder、build、push）。两者通过 `BuildSpec`（扁平契约）对接，`ImageBuilder` 不感知 `Image` 结构 —— `Image` 字段重组不影响 `ImageBuilder`。

2. **resolve-and-replace**：`Sandbox.start()` 入口处将 `Image` → `str` 并回写 `self.config.image`，下游 POST body、`__str__`、日志全部看到纯字符串，无需逐一改造。具体由 `Sandbox._resolve_image()` 完成。

3. **凭证沿 ImageRegistry 流动**：`Image.registry: ImageRegistry` 持有 `username`/`password`，`to_build_spec()` 投影到 `BuildSpec.registry_username`/`registry_password` 供 `ImageBuilder` 使用。`Sandbox.start()` 解析完成后将凭证同步到 `SandboxConfig` 供 Admin 拉取使用（仅在 caller 没显式设过的字段上覆盖）。

4. **4 段拼接命名**：命名一律 `{registry.url}/{registry.namespace}/{registry.repository}:{tag}`：
   - `registry.url` 默认 `ROCK_IMAGE_REGISTRY`（仅 host）
   - `registry.namespace` 默认 `ROCK_IMAGE_NAMESPACE`（`"rock"`）
   - `registry.repository` 默认 `SandboxConfig.user_id`（缺失 fallback `"default"`），在 `Sandbox.start()` 注入
   - `tag` 强制 = `content_hash()`，用户不可指定

5. **content_hash 作 tag**：内容变化自动产生新 tag → 缓存键不需要额外 label 对比。`docker build --label rock.content_hash=<hash>` 同时写入 label，作为 push 后的二次校验。

6. **缓存检查两层**：`docker manifest inspect` 看 image 是否已在 registry；存在则 `docker pull` + `docker inspect` 对比 label，匹配才跳过 build/push。

7. **Builder sandbox 可配置**：通过 `Image.from_dockerfile(..., builder_config=BuilderConfig(...))` 控制 builder 的 image / memory / cpus / timeouts。`BuilderConfig` 是 `SandboxConfig` 的子集，把 `image` 收窄为 `str`，默认值是 `ROCK_IMAGE_BUILDER_IMAGE`（默认 `rock-env-builder:latest`，基于通用 DinD 镜像，预配 daemon.json 适配 ROCK XFS 卷布局）。不传 `builder_config` 时，从用户 `SandboxConfig` 继承 `base_url` / `cluster` / `extra_headers` / `user_id` 等可继承字段。

---

## DinD 环境约束

`docker build` 在 Docker 23+ 默认走 BuildKit。我们保留 BuildKit（不强关），因此 builder 镜像需要满足它在"容器套容器"环境下的两个要求：

1. **干净的工作目录 FS** —— BuildKit 在 dockerd `data-root` 下做 overlay 挂载；若 data-root 在 sandbox 自己的 overlay rootfs 上，会触发 overlay-on-overlay 失败。
2. **干净的 dockerd 启动状态** —— base 镜像若残留 `/var/run/docker.pid`，新 dockerd 启动会被旧 PID 文件挡住。

builder 镜像通过 `daemon.json` + Dockerfile 清理满足这两点：

```json
{
  "data-root": "/data/logs/docker",
  "features": {"containerd-snapshotter": false}
}
```

- `data-root` 指向 `/data/logs/docker`，ROCK 平台已在此 bind-mount 一块 XFS 卷（本意是日志配额），底层非 overlay，BuildKit 的 overlay 挂载得以成功
- 关闭 `containerd-snapshotter` 让 BuildKit 走 dockerd 旧 graph driver，绕开独立 containerd-overlayfs 路径
- Dockerfile 里 `RUN rm -f /var/run/docker.pid /run/docker/containerd/containerd.pid` 清残留

整套配置封装在 builder 镜像里，SDK 仅通过 `ROCK_IMAGE_BUILDER_IMAGE` 引用 tag。

---

## Env Vars

| 变量 | 用途 | 默认 |
|---|---|---|
| `ROCK_IMAGE_REGISTRY` | 4 段拼接的 host 段 | `(空)` |
| `ROCK_IMAGE_NAMESPACE` | 4 段拼接的 namespace 段 | `"rock"` |
| `ROCK_IMAGE_REGISTRY_USERNAME` / `_PASSWORD` | 推/拉镜像凭证 | `(空)` |
| `ROCK_IMAGE_BUILDER_IMAGE` | 覆盖默认 builder sandbox 镜像 | `rock-env-builder:latest` |

---

## Tag 长度决策

采用 **OCI digest 标准长度（64 hex / 256 bit SHA-256，不截断）**。

理由：ROCK 场景属于 "registry-stored、跨进程持久化、无中心去重机制"，不像 Docker short ID（本地肉眼识别）或 Git short SHA（自动延长冲突解决）可以容忍截断。10⁻⁶⁵ 的碰撞概率与 OCI manifest digest 同一安全等级。

代价是 tag 长 ~70 字符，registry UI 不如短哈希好读——但用户主要通过 `Image.from_dockerfile()` 接口操作，不感知 tag 字符串。

位置选在 `:<tag>` 而非 OCI 标准 `@sha256:<digest>`：OCI digest 是 push 完成后 registry 端算的 manifest digest，我们需要在 build 前就能算出唯一标识做缓存键，所以必须用 build context 的 hash 放在 tag 位。

---

## Validation Plan

### 单元测试 — `tests/unit/sdk/sandbox/test_image.py`

| 用例 | 断言 |
|---|---|
| `_resolve_full_name` 缺段 | `ValueError`，message 列缺失字段 |
| `_resolve_full_name` 拼接正确 | `f"{reg}/{ns}/{repo}:{hash}"` |
| `registry.url` 末尾 `/` 被剥 | 不出现 `//` |
| env 默认生效 | `namespace` 默认 `"rock"` |
| Tag 格式 | `len(tag) == 64` 且 `[0-9a-f]{64}` |
| `from_dockerfile(file_path)` | 单文件 build context；同级文件不影响 hash |
| `from_dockerfile` 拒绝不存在的路径 | `ValueError` |
| `Sandbox.start()` 注入 `repository` | `None` 时 = `config.user_id` 或 `"default"` |

### 集成测试 — `tests/integration/sdk/sandbox/test_image_build.py`

| 用例 | 验证点 |
|---|---|
| `test_from_dockerfile_build_and_start` | from_dockerfile → start → `cat /opt/hello.txt` 验证 COPY 生效 |
| `test_from_dockerfile_cache_skip` | 相同 Image 第二次 build 命中缓存，耗时显著小于首次 |
| `test_from_dockerfile_rebuilds_on_content_change` | 修改 env_dir 内容触发重建，新内容生效 |

全部标 `@pytest.mark.need_admin`，CI 自动跑。

测试数据：`tests/integration/test_data/image_from_dockerfile/`（最小 `Dockerfile` + `hello.txt`）。

---

## Rollback

- 删除 `rock/sdk/sandbox/image.py`、`rock/sdk/sandbox/image_builder.py` 及对应测试
- 还原 `rock/sdk/sandbox/config.py`（`image: str | Image` → `image: str`）
- 还原 `rock/sdk/sandbox/client.py`（移除 `Image` 解析与 `repository` 注入）
- 还原 `rock/env_vars.py`（移除 `ROCK_IMAGE_NAMESPACE`、`ROCK_IMAGE_BUILDER_IMAGE`）
- Admin 侧无变更需回滚
