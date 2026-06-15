---
sidebar_position: 5
---

# 从 Dockerfile 启动沙箱

`SandboxConfig.image` 既可以传一个已构建好的镜像 tag 字符串，也可以传一个 `Image` 声明对象。使用 `Image.from_dockerfile(path)`，SDK 会在 builder sandbox 里透明完成构建和推送，再用构建出的镜像启动你的沙箱——不需要自己跑 `docker build` / `docker push`。

## 快速开始

```python
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig
from rock.sdk.sandbox.image import Image, ImageRegistry

image = Image.from_dockerfile(
    "/path/to/env_dir",                  # 包含 Dockerfile 的本地目录，或单个 Dockerfile 文件路径
    registry=ImageRegistry(
        url="reg.example.com",
        namespace="my-team",
        repository="my-env",
        username="...",
        password="...",
    ),
)

sandbox = Sandbox(SandboxConfig(image=image, memory="2g", cpus=1.0))
await sandbox.start()
```

`start()` 执行时 SDK 会：

1. 对 build context 计算 SHA-256 哈希（覆盖 `env_dir` 下所有文件）。
2. 查 registry 看是否已有同样哈希的镜像；命中则跳过 build + push。
3. 否则起一个 builder sandbox，在里面跑 `docker build` 和 `docker push`。
4. 用得到的镜像 tag 启动你的沙箱。

后续 `env_dir` 内容不变的话，直接命中缓存秒返回。

## 镜像命名

最终镜像 tag 由 4 段组成：

```
{registry.url}/{registry.namespace}/{registry.repository}:{content_hash}
```

| 段 | 来源 |
|---|---|
| `registry.url` | `ImageRegistry` 字段，缺省则从 admin `image` 配置获取 |
| `registry.namespace` | `ImageRegistry` 字段，缺省则从 admin `image` 配置获取 |
| `registry.repository` | `ImageRegistry` 字段，缺省则用 `SandboxConfig.user_id`（再缺省回退 `"default"`） |
| `content_hash` | build context 的 64 位 SHA-256，用户不可指定 |

把 content hash 放在 tag 位的设计：Dockerfile 或上下文文件任何变化都会自动产生新 tag，缓存命中与重建是确定性的。

## API 参考

```python
class ImageRegistry(BaseModel):
    url: str | None = None
    namespace: str | None = None
    repository: str | None = None
    username: str | None = None
    password: str | None = None


Image.from_dockerfile(
    path: str | Path,
    *,
    registry: ImageRegistry | None = None,
    force_build: bool = False,
    build_args: dict[str, str] | None = None,
    builder_config: BuilderConfig | None = None,
)
```

| 参数 | 作用 |
|---|---|
| `path` | 两种形式之一：(a) 包含 `Dockerfile` 以及它 `COPY` 引用的所有文件的本地目录，或 (b) 单个 `Dockerfile` 文件的路径。文件模式下同级目录其他文件不参与构建——Dockerfile 必须自包含（不能 `COPY` 本地文件） |
| `registry` | `ImageRegistry` POJO，包含推送目标和凭证。未设字段在 `Sandbox.start()` 时从 admin `image` 配置自动填充；`registry.repository` 回退到 `SandboxConfig.user_id`。镜像仓库凭证（username/password）通过 admin 服务的 ACR 临时 token 自动获取。 |
| `force_build` | 跳过缓存检查，强制重新构建 |
| `build_args` | 透传给 `docker build --build-arg KEY=VAL` |
| `builder_config` | `BuilderConfig`（`SandboxConfig` 的子类），用于 builder sandbox 自身——可控制 image、memory、cpus、timeouts 等。`BuilderConfig` 把 `image` 类型收窄到 `str`（pydantic 强制校验），默认值从 admin 配置获取 + builder 适用的 timeouts。不传时 builder 从你的 `SandboxConfig` 派生 |

不传 `builder_config` 时，builder sandbox 从你的 `SandboxConfig` 继承可继承字段（`base_url`、`cluster`、`extra_headers` 等）；`image` / `startup_timeout` / `auto_clear_seconds` 走 `BuilderConfig` 的默认值。

## 配置

镜像仓库和 builder 的默认配置在 admin 的 YAML 配置文件（`rock-conf/rock-*.yml`）中集中管理。SDK 客户端在 `Sandbox.start()` 时自动从 admin 的 `/acr_config` 接口获取——无需每个客户端单独配置。

```yaml
# rock-dev.yml
image:
  registry:
    url: "reg.example.com"
    namespace: "my-team"
    instance_id: "cri-xxxxxx"        # ACR 企业版实例 ID
    region: "cn-hangzhou"
    access_key_id: "..."             # 仅 admin 侧持有，不暴露给 SDK
    access_key_secret: "..."
  builder:
    image: "rock-n-roll-registry.cn-hangzhou.cr.aliyuncs.com/rock/rock-env-builder:latest"
    startup_timeout: 600
    auto_clear_seconds: 1800
```

镜像仓库凭证通过 admin 服务签发 ACR 临时 token（15 分钟有效期）。SDK 客户端不持有长期凭证。

## 自定义 builder 镜像

构建跑在一个短生命的 builder sandbox 里（容器里再起 dockerd，即 DinD）。默认的 builder 镜像已经预配好能在这种环境下工作；只有当你想在 admin 配置中替换它时，才需要看这一节。

builder 里的 `docker build` 默认走 BuildKit（Docker 23+）。"容器套容器"布局下 BuildKit 对镜像有两个要求：

1. **dockerd 的 data 目录所在文件系统不能是 overlay。** BuildKit 在 `<data-root>/buildkit/` 下挂 overlay；若 `data-root` 本身在 sandbox 的 overlay rootfs 上，会触发 overlay-on-overlay → `invalid argument`。
2. **镜像里不能有残留的 dockerd pidfile。** `/var/run/docker.pid` 或 `/run/docker/containerd/containerd.pid` 若被烤进镜像，新 dockerd 启动会被旧 PID 挡住，报 `process with PID N is still running`。

默认 builder 镜像通过以下方式同时满足这两条：

- `/etc/docker/daemon.json` 把 `data-root` 设为 `/data/logs/docker`。ROCK 给每个 sandbox 都会把宿主机一块 XFS 卷 bind-mount 到这里（本意是给日志配额用），所以 dockerd 数据落在 XFS 上，而非 overlay rootfs。
- `"features": {"containerd-snapshotter": false}` 让 BuildKit 走 dockerd 的旧 graph driver，而非独立的 containerd-overlayfs snapshotter。
- 镜像 build 时清掉残留 pidfile。

如果你自己 build builder 镜像，照搬这套配置即可；或者直接用默认 builder 完全不踩这个雷。

## 说明

- Builder sandbox 是短生命的：构建时按需起，构建完销毁。缓存在 registry 里，不在 builder 里。
- 同一 `Image` 第二次构建走 `docker manifest inspect` + content-hash label 校验，秒返回。
- 预构建镜像（不需要构建）的场景，直接把镜像 tag 字符串传给 `SandboxConfig.image` 即可——`Image` 仅在你需要 SDK 帮你跑 `docker build` + `docker push` 时使用。
