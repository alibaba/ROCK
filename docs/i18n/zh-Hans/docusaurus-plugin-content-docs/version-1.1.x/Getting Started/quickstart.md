---
sidebar_position: 2
---

# 快速上手

本指南将通过完整的示例演示如何使用 ROCK 创建和管理强化学习环境。ROCK (Reinforcement Open Construction Kit) 是一个全面的沙箱环境管理框架，主要用于强化学习和AI开发环境。

## 1. 环境准备

我们推荐在 Linux 系统下启动 ROCK，能够尽量复用项目依赖，提升环境拉起速度。如果需要在 macOS 上尝试，可以参考 [MacOS 启动](#7-macos-启动) 一节。

在开始之前，请确保您的系统已安装以下依赖项：

### 1.1 系统要求

- **Docker**: ROCK 使用 Docker 进行容器化环境管理
- **uv**: ROCK 使用 uv 进行依赖管理和虚拟环境创建

### 1.2 验证依赖安装

```bash
# 验证 Docker 安装
docker --version

# 验证 Docker 可用, 且示例中依赖python:3.11镜像
docker pull python:3.11

# 验证 uv 安装
uv --version


```

### 1.3 项目初始化

```bash
# 克隆项目仓库
git clone <repository>
cd ROCK

# 创建虚拟环境（使用 uv 托管的 Python, 以python 3.11 版本为例）
uv venv --python 3.11 --python-preference only-managed

# 安装所有依赖组
uv sync --all-extras
```

> **重要提示**: 为确保 ROCK 能正确挂载项目和虚拟环境及其依赖的 base Python 解释器，强烈推荐使用 uv 托管的 Python 环境而非系统 Python。

## 2. 激活虚拟环境

在运行任何 ROCK 命令之前，需要先激活虚拟环境。确保 sys.base_prefix 是 uv 管理的环境，类似于 `/root/.local/share/uv/python/cpython-3.11.8-linux-x86_64-gnu` 等路径。

```bash
# 激活虚拟环境
source .venv/bin/activate

# 验证 Python 环境
python -c "import sys; print('Base prefix:', sys.base_prefix)"
```

> **验证要点**: 确保输出的 base prefix 路径指向 uv 管理的 Python 环境，而非系统 Python。

## 3. 验证环境配置

激活虚拟环境后，验证依赖安装是否正确：

```bash
# 检查关键依赖
python -c "import rock; print(\"Hello ROCK\")"
```


## 4. 启动 ROCK 服务

激活虚拟环境后，在项目根目录下，启动 ROCK Admin 服务：

```bash
# 确保虚拟环境已激活
source .venv/bin/activate

# 启动 ROCK Admin 服务（本地环境）
rock admin start
```

服务启动后，您将看到类似以下的输出：

```
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8080 (Press CTRL+C to quit)
```

> **服务说明**: ROCK Admin 服务默认运行在 `http://127.0.0.1:8080`。

## 5. 运行示例环境

现在可以运行示例环境来验证安装。确保 ROCK 服务正在运行，然后打开一个新的终端窗口执行以下命令：

```bash
# 确保虚拟环境已激活
source .venv/bin/activate

# 运行沙箱示例
python examples/sandbox_demo.py

# 运行 GEM 协议示例
python examples/sokoban_demo.py
```

### 5.1 示例说明

- **sandbox_demo.py**: 演示如何使用 ROCK 的沙箱 SDK 创建和管理容器化环境
- **sokoban_demo.py**: 演示如何使用 ROCK 的 GEM 协议兼容接口创建强化学习环境

> **运行要求**: 确保 ROCK Admin 服务正在运行，因为示例需要与服务进行通信。

## 6. 分布式环境配置（可选）

对于分布式多机器环境，请确保以下配置一致：

1. 所有机器上 ROCK 和 uv 的 Python 配置使用相同的根 Python 解释器
2. Docker 版本在所有节点上保持一致
3. 网络配置允许各节点间正常通信



## 7. MacOS 启动

在 macOS 上，如果需要启动 Linux 镜像的环境，需要先设置环境变量：

```bash
export ROCK_WORKER_ENV_TYPE=uv
```

在容器启动时，会安装对应的 uv 环境，细节可以参考 `rock/rocklet/local_files/docker_run_with_uv.sh` 脚本。

> **注意**: 相比 Linux 系统，macOS 上的启动速度会较慢，且比较依赖网络环境，可以根据实际情况调整脚本。ROCK_WORKER_ENV_TYPE的细节可以参考 [Configuration Guide](../User%20Guides/configuration.md).


## 8. 从Pip源启动

如果从Pip源启动Admin Server，在参照[安装指南](./installation.md)安装完成ROCK后, 需要设置额外环境变量: 

```bash
export ROCK_WORKER_ENV_TYPE=pip
```

(这一启动方式在容器环境启动时会从Pypi源上拉取最新的rocklet并安装, 相对启动速度比较慢, 仅推荐测试使用, 生产上依旧推荐其他的启动方式)


## 总结

恭喜！您已经成功完成了 ROCK 的快速开始指南。现在您应该能够：

- 正确设置 ROCK 开发环境
- 使用 uv 管理的 Python 环境
- 启动和管理 ROCK 服务
- 运行示例程序验证安装
- 在分布式环境中配置 ROCK（如果需要）

如需深入了解 ROCK 的更多功能，请参考以下文档：

## 下一步学习

- [配置指南](../User%20Guides/configuration.md) - 详细了解 ROCK 的配置选项
- [API 文档](../References/api.md) - 查看完整的 API 接口
- [Python SDK 文档](../References/Python%20SDK%20References/python_sdk.md) - 学习如何使用 Python SDK 进行开发
- [安装指南](./installation.md) - 详细了解 ROCK 安装和配置
- [概览](../overview.md) - 了解 ROCK 的设计理念