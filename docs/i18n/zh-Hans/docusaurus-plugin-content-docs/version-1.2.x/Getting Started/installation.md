---
sidebar_position: 3
---

# 安装指南

本文档介绍如何使用 `uv` 和 `pip` 安装和设置 ROCK 开发环境。该项目是一个强化学习开放构建工具包，支持多种组件。

## 使用 uv（推荐）

### 快速安装所有依赖

```bash
# 安装所有依赖（包括可选依赖）
uv sync --all-extras

# 安装开发/测试依赖
uv sync --all-extras --all-groups
```

### 安装不同依赖组

#### 仅核心依赖
```bash
uv sync
```

#### 管理组件依赖
```bash
uv sync --extra admin
```

#### Rocklet 执行环境依赖
```bash
uv sync --extra rocklet
```

#### 所有依赖
```bash
uv sync --all-extras
```

#### 开发/测试依赖
```bash
uv sync --all-extras --group test
```

## 使用 pip

### 从 pip 源安装

#### 仅核心依赖
```bash
pip install rl-rock
```

#### 管理组件依赖
```bash
pip install "rl-rock[admin]"
```

#### Rocklet 执行环境依赖
```bash
pip install "rl-rock[rocklet]"
```

#### 构建器依赖
```bash
pip install "rl-rock[builder]"
```

#### 安装所有可选依赖
```bash
pip install "rl-rock[all]"
```

### 使用 pip 从源码安装

#### 仅核心依赖
```bash
pip install .
```

#### 管理组件依赖
```bash
pip install ".[admin]"
```

#### Rocklet 执行环境依赖
```bash
pip install ".[rocklet]"
```

#### 构建器依赖
```bash
pip install ".[builder]"
```

#### 安装所有可选依赖
```bash
pip install ".[all]"
```

## 可用入口点

该包提供以下命令行脚本：

- `rocklet`: ROCK 执行环境服务器 (rock.rocklet.server:main)
- `admin`: 管理服务器 (rock.admin.main:main)
- `envhub`: 环境中心服务器 (rock.envhub.server:main)
- `rock`: 主 ROCK 命令行接口 (rock.cli.main:main)

## 开发设置

### 使用 uv（推荐）

```bash
# 克隆并设置开发环境
git clone <repository>
cd ROCK
uv sync --all-extras --group test

# 运行测试
uv run pytest


### 使用 pip

```bash
# 开发模式安装所有可选依赖
pip install -e ".[all]"

# 分别安装
pip install -e .
pip install ".[admin]" ".[rocklet]" ".[builder]"
```

## 附加说明

- 项目配置为默认使用阿里云 PyPI 镜像: `https://mirrors.aliyun.com/pypi/simple/`
- 对于本地开发，运行测试需要 `test` 依赖组
