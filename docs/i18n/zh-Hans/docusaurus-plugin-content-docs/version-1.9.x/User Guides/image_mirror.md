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

默认情况下，`rockcli image mirror` 将镜像推送到**新加坡**仓库，然后由 ACR 内置的跨区域同步机制自动复制到上海。

> **注意：** ACR 跨区域同步在高负载时可能出现任务阻塞或延迟。如果需要镜像立即在上海可用，可以通过指定 `--cluster vpc-nt-a` 使用 remote 模式直接转储到上海仓库（参见[直接转储到上海](#直接转储到上海)）。

## 前置条件

安装最新版本的 `rockcli`：

```bash
bash -c "$(curl -fsSL http://xrl.alibaba-inc.com/install_beta.sh)"
```

验证安装：

```bash
rockcli --help
```

## 命令参考

### `rockcli image mirror`

将镜像从源仓库转储到 ROCK 目标仓库。

```bash
rockcli image mirror <image> [<image>...] [options]
```

也可通过 `-f <path>` 提供镜像列表（支持 `txt`、`jsonl`、`skopeo` 格式）。

#### 关键选项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--mode` | `skopeo` | `skopeo`（推荐，在沙箱中 skopeo copy）、`remote`（在沙箱中 docker pull/tag/push）、`local`（本机 Docker） |
| `--concurrency` | *（自动）* | `skopeo`/`remote`：沙箱数量；`local`：本机并行镜像数 |
| `-c, --cluster` | *（无）* | 集群路由提示。`vpc-sg-*` 走国外 ACR，其他 `vpc-*` 走国内 ACR |
| `--source-username` | *（无）* | 源仓库用户名 |
| `--source-password` | *（无）* | 源仓库密码 |
| `--target-registry` | 内置 | 目标仓库地址，显式传入时覆盖区域默认仓 |
| `--resume` | `false` | 从进度文件恢复 |

> **说明：** 目标仓库凭证已内置于 `rockcli` 中。大多数情况下只需提供源镜像即可开始转储。完整选项请运行 `rockcli image mirror --help`。

## 使用示例

### 转储公共镜像

对于来自公共仓库（Docker Hub 等）的镜像，无需源仓库认证：

```bash
rockcli image mirror rex-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11
```

同时转储多个镜像：

```bash
rockcli image mirror rex-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11 ubuntu:22.04
```

### 转储私有镜像

当源镜像需要认证时，提供源仓库凭证：

```bash
rockcli image mirror ghcr.io/my-org/my-image:v1.0 \
  --source-username <your_source_username> \
  --source-password <your_source_password>
```

### 使用本地 Docker 转储

使用 `local` 模式通过本机 Docker 守护进程转储，而非沙箱：

```bash
rockcli image mirror rex-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11 --mode local
```

### 直接转储到上海

如果 ACR 跨区域同步出现阻塞或延迟，可以通过 `--cluster vpc-nt-a` 直接转储到上海仓库：

```bash
rockcli image mirror rex-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11 \
  --cluster vpc-nt-a \
  --target-registry rock-instances-registry.cn-hangzhou.cr.aliyuncs.com
```

## 使用转储后的镜像

转储完成后，`rockcli` 会输出转储后的镜像地址。创建沙箱时请使用该地址。

## 工作原理

1. **解析** —— 解析命令行提供的源镜像（或通过 `-f` 从文件读取）。
2. **检查** —— 登录目标仓库，检查镜像是否已存在。如果已存在则跳过（不支持覆盖）。
3. **拉取** —— 从源仓库拉取镜像（如提供了源仓库凭证，会先登录）。
4. **打标签** —— 将镜像重新打标签为目标仓库地址，保留原始的 namespace、镜像名和 tag。
5. **推送** —— 将重新打标签的镜像推送到目标仓库。

每个镜像转储操作失败后最多重试 3 次。

> **注意：** 目标仓库不支持覆盖已存在的镜像。如果需要更新镜像内容，请修改 tag 后重新转储。

### 镜像名称映射

原始镜像名称映射到目标仓库时，保留其原有结构：

```
源镜像: rex-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11
目标:   rock-instances-registry.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11

源镜像: ghcr.io/my-org/my-image:v1.0
目标:   rock-instances-registry.ap-southeast-1.cr.aliyuncs.com/my-org/my-image:v1.0
```

## 转储结果

转储结果保存在 `data/output/env-build/result.jsonl`。每行包含原始实例记录，并附加两个字段：

| 字段 | 说明 |
|------|------|
| `rock_env_build_result` | `SUCCESS` 或 `FAILED` |
| `rock_env_build_message` | 成功信息或错误堆栈 |
