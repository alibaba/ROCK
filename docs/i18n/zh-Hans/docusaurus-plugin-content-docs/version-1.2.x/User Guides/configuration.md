---
sidebar_position: 4
---

# 配置指南

本指南详细介绍如何配置 ROCK 环境以满足不同的使用需求，包括本地开发、测试和生产部署。

## 1. 环境变量配置

ROCK 支持通过环境变量配置关键参数。以下是主要的环境变量：

```bash
export ROCK_BASE_URL=http://localhost:8080  # ROCK服务基础URL
export ROCK_LOG_LEVEL=INFO  # 日志级别
export ROCK_LOGGING_PATH=/path/to/logs  # 日志文件路径，默认 None (输出到控制台)
export ROCK_LOGGING_FILE_NAME=rocklet.log  # 日志文件名，默认 "rocklet.log", 启动admin时可以自定义日志文件名, 如admin.log
export ROCK_LOGGING_LEVEL=INFO  # 日志输出级别，默认 "INFO"
export ROCK_WORKER_ENV_TYPE=local  # 运行时环境类型，可选值: local, docker, uv, pip
```

更多环境变量可参考 `rock/env_vars.py` 文件。

### 1.1 运行时环境 (Runtime Environments)

ROCK 提供了多种不同的运行时环境来满足不同场景的需求，选择通过环境变量 `ROCK_WORKER_ENV_TYPE` 进行配置。每种环境有不同的部署要求、性能特征和适用场景。每种环境都有其独特的优势和限制，开发者可以根据部署环境的需要选择最适合的运行时环境。

#### 1.1.1 Docker 运行时环境

Docker 运行时环境适用于已经预安装了所需依赖的 Docker 镜像环境。这种环境要求部署环境中直接可用 `/tmp/miniforge/bin/rocklet` 可执行文件。

**挂载配置:**
- `/tmp/miniforge` - 包含预安装的 Python 环境
- `/tmp/local_files` - 包含执行所需的本地文件

**启动命令:**
```bash
chmod +x /tmp/local_files/docker_run.sh && /tmp/local_files/docker_run.sh
```

**适用场景:**
- 容器化部署环境
- 已经构建了包含 `rocklet` 的自定义 Docker 镜像
- 适合生产环境，启动速度快

**要求:**
- 需要使用定制的 Docker 镜像，其中包含 `/tmp/miniforge/bin/rocklet` 可执行文件
- Docker 环境支持

#### 1.1.2 本地运行时环境

本地运行时环境直接利用当前部署环境的 Python 环境和项目文件。该环境要求宿主机和容器之间具有相同的操作系统，以便能够直接挂载虚拟环境和 Python 解释器。

**挂载配置:**
- `python_env_path` - Python 环境路径
- `project_root` - 项目根目录
- `.venv` - 虚拟环境目录（挂载为容器中的 `/tmp/miniforge`）
- `local_files` - 执行所需的本地文件

**启动命令:**
```bash
chmod +x /tmp/local_files/docker_run.sh && /tmp/local_files/docker_run.sh
```

**适用场景:**
- 开发环境
- 宿主机和目标容器使用相同操作系统的场景
- 需要快速重新使用现有 Python 环境

**要求:**
- 相同的操作系统（主机/容器）
- 可直接访问当前部署的 `.venv` 虚拟环境
- Python 解释器路径兼容

#### 1.1.3 UV 运行时环境

UV 运行时环境只依赖于可用的 ROCK 项目，但初始化相对较慢且网络要求较高。这种环境最适合没有预配置环境的场景。它从原始项目重新构建 rocklet 环境。这是推荐在 Mac 操作系统上使用的环境。

**挂载配置:**
- `project_root` - 项目根目录（挂载为容器中的 `/tmp + project_root`）
- `local_files` - 执行所需的本地文件

**启动命令:**
```bash
chmod +x /tmp/local_files/docker_run_with_uv.sh && /tmp/local_files/docker_run_with_uv.sh '<container_project_root>'
```

**适用场景:**
- Mac 操作系统
- 跨操作系统启动
- 没有预配置环境的场景
- 没有使用 uv 管理 Rock

**优势:**
- 无需预构建镜像
- 跨平台兼容性好
- 特别适合开发和测试

**限制:**
- 初始化速度较慢
- 网络要求较高
- 启动时间较长

#### 1.1.4 PIP 运行时环境

PIP 运行时环境使用 pip 在容器内安装所需依赖。这种环境适合快速设置并能在容器中完成依赖安装的场景，是默认的运行时环境。它不需要预先构建包含依赖的镜像，通过 pip 直接管理 Python 包。

**挂载配置:**
- `local_files` - 包含执行所需的本地文件

**启动命令:**
```bash
chmod +x /tmp/local_files/docker_run_with_pip.sh && /tmp/local_files/docker_run_with_pip.sh
```

**适用场景:**
- 使用PIP源安装的ROCK
- 快速测试ROCK

**优势:**
- 简单的部署设置

**限制:**
- 依赖安装时间较长
- 需要网络访问以安装依赖包
- 每次启动时都需要安装依赖

#### 1.1.5 配置指南

根据不同的使用场景，可以参考以下选择指南：

| 场景 | 推荐环境 | 原因 |
|------|----------|------|
| 生产环境 | Docker 运行时 | 快速启动，稳定性能 |
| 开发环境，同一 OS | 本地运行时 | 环境重用，开发周期快 |
| Mac 开发 | UV 运行时 | 支持最佳的跨平台兼容性 |
| 跨平台开发 | UV 运行时 | 避免环境兼容性问题 |
| 快速测试 | UV 运行时 | 无需预配置工作 |
| PIP源安装 | PIP 运行时 | 直接使用 pip 安装依赖 |

这些运行时环境通过 `ROCK_WORKER_ENV_TYPE` 环境变量进行配置，该变量可设置为 "local"、"docker"、"uv" 或 "pip"。

### 1.2 日志配置

在日志配置方面，ROCK 的日志系统具有以下特性：

- 日志系统不能同时输出到文件和控制台，只有当设置了 `ROCK_LOGGING_PATH` 时，日志才会输出到指定文件，否则输出到控制台。
- `ROCK_LOGGING_LEVEL` 用于控制日志输出级别，`ROCK_LOG_LEVEL` 用于通用日志级别设置。

## 2. 分布式部署要求

由于 ROCK 支持分布式部署，当在 Ray 集群的不同节点上运行时，需要满足以下一致性要求：

#### 目录结构一致性
在所有 Ray 节点上，必须保证以下目录结构完全一致：
- ROCK 项目仓库目录
- `.venv` 虚拟环境目录
- `.venv` 依赖的 base Python 目录


#### 挂载要求
ROCK 的启动依赖于挂载 ROCK 项目和对应的 base Python 环境，要求在多机环境中保持一致性：

#### 验证分布式配置
可以通过以下方式验证分布式部署配置：

```bash
# 在所有节点上检查目录一致性
ls -la /path/to/rock
ls -la /path/to/rock/.venv
ls -la $ROCK_PYTHON_ENV_PATH

# 验证 Python 环境可用性
$ROCK_PYTHON_ENV_PATH/bin/python --version

# 检查所有节点上的环境变量设置
echo $ROCK_PYTHON_ENV_PATH
echo $ROCK_PROJECT_ROOT
```



## 相关文档

- [快速开始指南](../Getting%20Started/quickstart.md) - 了解如何快速搭建 ROCK 环境
- [API 文档](../References/api.md) - 查看沙箱相关的 API 接口
- [Python SDK 文档](../References/Python%20SDK%20References/python_sdk.md) - 学习如何使用 SDK 配置沙箱
- [安装指南](../Getting%20Started/installation.md) - 详细了解 ROCK 安装和配置
