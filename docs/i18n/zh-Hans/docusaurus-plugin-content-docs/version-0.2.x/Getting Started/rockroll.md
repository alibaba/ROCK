---
sidebar_position: 7
---

# ROCK & ROLL 快速开始指南

本指南将引导您使用 ROLL (训练框架) 和 ROCK (环境管理) 来运行一个基于 Sokoban 游戏（推箱子）的强化学习训练示例。

## 1. 单机环境准备

在开始之前，请先确保您的系统已安装以下依赖项：

### 1.1 系统要求

- **操作系统**: 推荐使用 Linux (如 Ubuntu 20.04+)
- **硬件**: 建议使用 NVIDIA GPU 并安装对应的驱动程序
- **Docker**: ROCK 使用 Docker 进行容器化环境管理
- **uv**: ROCK 使用 uv 进行依赖管理和虚拟环境创建

### 1.2 验证依赖安装

```bash
# 验证 Docker 安装
docker --version

# 验证 Docker 可用, 且可提前拉取 Sokoban 游戏环境镜像，避免训练时等待
docker pull rock-n-roll-registry.cn-hangzhou.cr.aliyuncs.com/rock/sokoban-sandbox:latest

# 验证 uv 安装
uv --version

```

### 1.3 项目初始化

```bash
# 克隆项目仓库
git clone https://github.com/alibaba/ROCK.git
git clone https://github.com/alibaba/ROLL.git

# 确保两个仓库位于同一级目录下，如下所示：
# your-workspace/
# ├── ROCK/
# └── ROLL/
```


## 2. 启动训练流程

> 说明：下文均以 *torch2.6.0 + vLLM0.8.4* 为例。  


### 方式一： 使用虚拟环境启动（推荐）

#### 为什么推荐这种方式？
- 隔离性：uv 虚拟环境能确保项目依赖与系统环境隔离，避免冲突。
- 速度快：ROCK 可以复用此虚拟环境，大大加快了后续环境的启动速度。
- 稳定性：依赖关系更清晰，环境更易复现。 


```bash  
# 进入 ROCK 目录
cd ROCK

# 使用 uv 创建并激活 Python 3.10 虚拟环境（ROLL推荐使用Python 3.10）
uv venv --python 3.10 --python-preference only-managed

# 激活虚拟环境
source .venv/bin/activate

# 使用uv安装ROCK的依赖
uv sync --all-extras

# 若使用Python 3.10, 启动 ray 时会报错：ValueError: <object object at 0x...> is not a valid Sentinel
# 原因是 ray 与 click>=8.3 版本不兼容，需要降级到 click<8.3
# Python 3.11 不会有这个问题
uv pip install 'click>=8.2,<8.3'

# 切换到 ROLL 目录以安装其依赖
cd ../ROLL

# 设置国内 PyPI 镜像源以加速下载
PYPI_MIRROR="https://mirrors.aliyun.com/pypi/simple/"

# 安装核心 PyTorch 组件
uv pip install torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 -i $PYPI_MIRROR

# 安装transformer-engine，--no-build-isolation 避免因环境隔离导致找不到 torch
uv pip install transformer-engine[pytorch]==2.2.0 --no-build-isolation -i $PYPI_MIRROR 

# 安装预编译的 flash-attention，以匹配特定的 CUDA 和 PyTorch 版本
uv pip install https://github.com/Dao-AILab/flash-attention/releases/download/v2.7.2.post1/flash_attn-2.7.2.post1+cu12torch2.6cxx11abiFALSE-cp310-cp310-linux_x86_64.whl  

# 安装其余依赖
uv pip install -r requirements_torch260_vllm.txt -i $PYPI_MIRROR

# (可选) 安装Tensorboard，用于查看训练指标
uv pip install tensorboard -i $PYPI_MIRROR

# 启动ROLL脚本（包含ROCK服务的启动）
bash examples/agentic_demo/run_agentic_pipeline_sokoban_sandbox_single_node.sh
```

### 方式二：使用系统环境启动（备选方案）

为获得最佳兼容性，推荐使用 ROLL 官方提供的基础 Docker 镜像，因为它们已经预装了匹配的 CUDA、cuDNN 和其他基础库。

> [ROLL 官方镜像列表](https://alibaba.github.io/ROLL/docs/English/QuickStart/)  


#### 注意
此方式会将所有 Python 包直接安装到您的当前环境（例如，容器的基础环境）中，可能会与系统自带的包或其他项目产生冲突。

由于 ROCK 无法复用环境，每次启动任务时都可能需要重新安装部分依赖，启动速度较慢且受网络影响。


```bash  
PYPI_MIRROR="https://mirrors.aliyun.com/pypi/simple/"

# 安装ROCK的依赖
cd ROCK
pip install . -i $PYPI_MIRROR
pip install ".[admin]" -i $PYPI_MIRROR

# 安装ROLL的依赖
cd ../ROLL
pip install -r requirements_torch260_vllm.txt -i $PYPI_MIRROR

# 配置ROCK用uv启动的环境变量
export ROCK_WORKER_ENV_TYPE=uv

# 启动ROLL脚本（包含ROCK服务的启动）
bash examples/agentic_demo/run_agentic_pipeline_sokoban_sandbox_single_node.sh
```

至此，您已成功启动了 Sokoban 强化学习训练流程。祝您 Rock & Roll 愉快！


## 3. 多机部署

除了在单机上运行，您也可以将 **ROCK 服务** 和 **ROLL 训练** 部署在不同的机器上，通过网络进行通信。这是一种常见的服务化部署模式。

### 3.1 在机器 A 上部署 ROCK 服务

在一台独立的机器（或容器）上，参照[ROCK快速指南](https://rock.io.alibaba-inc.com/docs/rock/CN/quickstart)部署并启动 ROCK 服务。

> **重要提示**  
> 启动服务后，请记下ROCK服务的IP地址和端口，例如`http://192.168.1.10:8000`，后续步骤将需要这个地址。

### 3.2 在机器 B 上准备 ROLL 客户端

在另一台将要运行训练任务的机器上，执行以下操作。

1. 验证网络连通性

首先，使用 curl 命令检查是否能从机器 B 访问到机器 A 上的 ROCK 服务。
```bash  
# 将 <ip_address>:<port> 替换为您的 ROCK 服务实际地址
# 如果成功，会收到 ROCK 服务的响应 {"message":"hello, ROCK!"}
curl http://<ip_address>:<port>
```

2. 准备 ROLL 环境

```bash  
# 克隆 ROLL 仓库
git clone https://github.com/alibaba/ROLL.git
cd ROLL

# 安装依赖
pip install -r requirements_torch260_vllm.txt -i https://mirrors.aliyun.com/pypi/simple/
```

3. 配置 ROLL 连接地址

修改 ROLL 的配置文件，使其能够找到并连接到远程的 ROCK 服务。
- 打开配置文件：examples/agentic_demo/agentic_val_sokoban_sandbox.yaml
- 找到 SokobanSandbox 下的 env_config 部分
- 将 base_url 的值修改为您的 ROCK 服务地址
```yaml
custom_envs:
  SokobanSandbox:
      env_config:
          # 将这里的地址修改为您的 ROCK 服务地址
          # 例如: base_url: 'http://192.168.1.10:8000'
          base_url: 'http://<ip_address>:<port>'
```

4. 启动训练
配置完成后，即可在机器 B 上启动 ROLL 训练脚本。

```bash
# 此脚本现在会通过网络请求机器 A 上的 ROCK 服务来创建环境  
bash examples/agentic_demo/run_agentic_pipeline_sokoban_sandbox_multi_nodes.sh
```

### 进阶：分布式 ROLL 训练

如果您希望将 ROLL 训练任务本身进行分布式部署，可以参考 ROLL 的官方分布式部署文档。
> [快速上手：多节点部署指南](https://alibaba.github.io/ROLL/zh-Hans/docs/Getting%20Started/Quick%20Start/multi_nodes_quick_start)