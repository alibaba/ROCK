---
sidebar_position: 6
---

# 镜像转储

ROCK 使用统一的 ACR（阿里云容器镜像服务）—— **rock-instances** —— 来管理沙箱镜像。使用自定义 Docker 镜像时，必须先将镜像转储到 ROCK 镜像仓库，才能在沙箱中使用。

## 仓库区域

**rock-instances** ACR 部署在两个区域：

| 区域 | 仓库地址 | 角色 |
|------|---------|------|
| 新加坡 (ap-southeast-1) | `rock-instances-registry.ap-southeast-1.cr.aliyuncs.com` | 默认转储目标 |
| 上海 (cn-hangzhou) | `rock-instances-registry.cn-hangzhou.cr.aliyuncs.com` | 通过 ACR 跨区域同步自新加坡 |

默认情况下，`rock image mirror` 将镜像推送到**新加坡**仓库，然后由 ACR 内置的跨区域同步机制自动复制到上海。

> **注意：** ACR 跨区域同步在高负载时可能出现任务阻塞或延迟。如果需要镜像立即在上海可用，可以通过指定 `--cluster vpc-nt-a` 使用 remote 模式直接转储到上海仓库（参见[直接转储到上海](#直接转储到上海)）。

## 前置条件

安装最新版本的 `rockcli`：

```bash
bash -c "$(curl -fsSL http://xrl.alibaba-inc.com/install_beta.sh)"
```

验证安装：

```bash
rock --help
```

## 准备镜像列表

`rock image mirror` 命令读取一个 JSONL 文件，每行是一个包含 `docker_image` 字段的 JSON 对象，遵循 SWE-bench 实例格式。

**示例文件**（`images.jsonl`）：

```jsonl
{"instance_id": "example_1", "docker_image": "docker.io/library/python:3.11"}
{"instance_id": "example_2", "docker_image": "ghcr.io/my-org/my-image:v1.0"}
{"instance_id": "example_3", "docker_image": "ubuntu:22.04"}
```

`docker_image` 字段必须是完整的镜像引用，包含 registry（如非 Docker Hub）、namespace、镜像名和 tag。如果未指定 tag，默认使用 `latest`。

## 命令参考

### `rock image mirror`

将镜像从源仓库转储到 ROCK 目标仓库。

```bash
rock image mirror -f <file> \
  [--target-registry <target_registry_url>] \
  [--target-username <target_username>] \
  [--target-password <target_password>] \
  [--source-registry <source_registry_url>] \
  [--source-username <source_username>] \
  [--source-password <source_password>] \
  [--mode <local|remote>] \
  [--concurrency <1-50>]
```

#### 必填参数

| 参数 | 说明 |
|------|------|
| `-f, --file` | 包含镜像列表的 JSONL 文件路径 |

#### 可选参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--target-registry` | 内置（rock-instances 新加坡） | 目标 ACR 仓库地址，默认指向 ROCK 镜像仓库，通常无需指定 |
| `--target-username` | 内置 | 目标仓库用户名，默认使用内置的 ROCK ACR 凭证 |
| `--target-password` | 内置 | 目标仓库密码，默认使用内置的 ROCK ACR 凭证 |
| `--source-registry` | *（无）* | 源仓库地址。仅当源仓库需要认证时使用 |
| `--source-username` | *（无）* | 源仓库用户名 |
| `--source-password` | *（无）* | 源仓库密码 |
| `--mode` | `local` | 转储模式：`local`（在本机执行）或 `remote`（在 ROCK 沙箱中分布式执行） |
| `--concurrency` | `3` | 并发转储任务数（1–50），仅在 `remote` 模式下生效 |

> **说明：** `--target-registry`、`--target-username` 和 `--target-password` 已内置于 `rockcli` 中，默认指向 ROCK ACR（rock-instances）。大多数情况下只需提供 `-f` 即可开始转储。

## 使用示例

### 转储公共镜像（Local 模式）

对于来自公共仓库（Docker Hub 等）的镜像，无需源仓库认证，只需提供镜像列表文件即可：

```bash
rock image mirror -f images.jsonl
```

目标仓库凭证将自动使用内置默认值。

### 转储私有镜像（Local 模式）

当源镜像需要认证时，提供源仓库凭证：

```bash
rock image mirror -f images.jsonl \
  --source-registry ghcr.io \
  --source-username <your_source_username> \
  --source-password <your_source_password>
```

### 转储镜像（Remote 模式）

对于大规模镜像转储，使用 `remote` 模式将任务分发到多个 ROCK 沙箱中执行。需要通过全局参数或配置文件配置 `--auth-token` 和 `--cluster`：

```bash
rock --auth-token <token> --cluster <cluster_name> \
  image mirror -f images.jsonl \
  --mode remote \
  --concurrency 10
```

### 直接转储到上海

如果 ACR 跨区域同步出现阻塞或延迟，可以绕过同步机制，直接转储到上海仓库。指定 `--cluster vpc-nt-a` 在上海集群上执行远程任务，并覆盖 `--target-registry` 为上海地址：

```bash
rock --auth-token <token> --cluster vpc-nt-a \
  image mirror -f images.jsonl \
  --mode remote \
  --concurrency 10 \
  --target-registry rock-instances-registry.cn-hangzhou.cr.aliyuncs.com
```

## 工作原理

1. **解析** —— 逐行读取 JSONL 文件，提取 `docker_image` 字段。
2. **检查** —— 登录目标仓库，检查镜像是否已存在。如果已存在则跳过。
3. **拉取** —— 从源仓库拉取镜像（如提供了源仓库凭证，会先登录）。
4. **打标签** —— 将镜像重新打标签为目标仓库地址，保留原始的 namespace、镜像名和 tag。
5. **推送** —— 将重新打标签的镜像推送到目标仓库。

每个镜像转储操作失败后最多重试 3 次。

### 镜像名称映射

原始镜像名称映射到目标仓库时，保留其原有结构：

```
源镜像: ghcr.io/my-org/my-image:v1.0
目标:   rock-instances-registry.ap-southeast-1.cr.aliyuncs.com/my-org/my-image:v1.0

源镜像: docker.io/library/python:3.11
目标:   rock-instances-registry.ap-southeast-1.cr.aliyuncs.com/library/python:3.11
```

## 转储结果

转储结果保存在 `data/output/env-build/result.jsonl`。每行包含原始实例记录，并附加两个字段：

| 字段 | 说明 |
|------|------|
| `rock_env_build_result` | `SUCCESS` 或 `FAILED` |
| `rock_env_build_message` | 成功信息或错误堆栈 |
