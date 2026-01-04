---
sidebar_position: 1
---

# API 参考

本指南详细介绍 ROCK 平台提供的核心 API 服务，包括沙箱环境管理和 GEM 环境交互。

## 目录

- [API 参考](#api-参考)
  - [目录](#目录)
  - [1. 概述](#1-概述)
  - [2. Sandbox API](#2-sandbox-api)
    - [沙箱管理接口](#沙箱管理接口)
    - [命令执行接口](#命令执行接口)
    - [文件操作接口](#文件操作接口)
  - [3. GEM API](#3-gem-api)
  - [相关文档](#相关文档)
  - [4. HTTP API 使用示例](#4-http-api-使用示例)
    - [4.1 Sandbox API 示例](#41-sandbox-api-示例)
      - [启动沙箱](#启动沙箱)
      - [异步启动沙箱](#异步启动沙箱)
      - [执行命令](#执行命令)
      - [创建会话](#创建会话)
      - [在会话中执行命令](#在会话中执行命令)
      - [上传文件](#上传文件)
      - [停止沙箱](#停止沙箱)
    - [4.2 GEM API 示例](#42-gem-api-示例)

## 1. 概述

ROCK平台提供两种核心API服务：
- Sandbox API：沙箱环境管理
- GEM API：GEM环境交互

所有 API 接口都遵循 RESTful 设计原则，支持 JSON 格式的数据交换。

## 2. Sandbox API

沙箱环境全生命周期管理功能：

### 沙箱管理接口

1. **Start Sandbox** - 启动沙箱环境
   - 创建一个新的沙箱实例
   - 支持指定镜像、资源配置等参数

2. **Start Sandbox Async** - 异步启动沙箱环境
   - 异步方式创建沙箱实例
   - 适用于需要快速响应的场景

3. **Check Sandbox Alive Status** - 检查沙箱存活状态
   - 验证沙箱是否正常运行

4. **Get Sandbox Statistics** - 获取沙箱统计信息
   - 获取沙箱的资源使用统计

5. **Get Sandbox Status** - 获取沙箱详细状态
   - 获取沙箱的完整状态信息

6. **Stop Sandbox** - 停止沙箱环境
   - 安全关闭沙箱实例

7. **Commit Sandbox** - 提交沙箱为镜像
   - 将当前沙箱状态保存为新镜像

### 命令执行接口

8. **Execute Command** - 在沙箱中执行命令
   - 直接在沙箱中运行指定命令

9. **Create Bash Session** - 创建Bash会话
   - 创建持久化的Bash会话环境

10. **Run Command in Session** - 在会话中执行命令
    - 在已创建的会话中执行命令

11. **Close Session** - 关闭会话
    - 释放会话资源

### 文件操作接口

12. **Read File** - 读取沙箱文件
    - 从沙箱中读取指定文件内容

13. **Write File** - 写入沙箱文件
    - 向沙箱中写入文件

14. **Upload File** - 上传文件到沙箱
    - 将本地文件上传到沙箱

## 3. GEM API

GEM环境交互功能：

1. **Make Environment** - 创建GEM环境
   - 初始化一个新的GEM环境实例

2. **Reset Environment** - 重置GEM环境
   - 将GEM环境重置到初始状态

3. **Step Environment** - 执行GEM环境步骤
   - 在GEM环境中执行一个动作步骤

4. **Close Environment** - 关闭GEM环境
   - 释放GEM环境资源

## 相关文档

- [快速开始指南](../Getting%20Started/quickstart.md) - 了解如何快速开始使用 ROCK API
- [Python SDK 文档](./Python%20SDK%20References/python_sdk.md) - 学习如何使用 SDK 调用 API
- [配置指南](../User%20Guides/configuration.md) - 了解 API 相关的配置选项
- [安装指南](../Getting%20Started/installation.md) - 详细了解 ROCK 安装和配置


## 4. HTTP API 使用示例

### 4.1 Sandbox API 示例

#### 启动沙箱
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/start' \
-H 'Content-Type: application/json' \
-d '{
  "image": "python:3.11",
  "resources": {
    "cpu": "2",
    "memory": "8g"
  }
}'
```

#### 异步启动沙箱
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/start_async' \
-H 'Content-Type: application/json' \
-d '{
  "image": "python:3.11",
  "resources": {
    "cpu": "2",
    "memory": "8g"
  }
}'
```

#### 执行命令
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/execute' \
-H 'Content-Type: application/json' \
-d '{
  "sandbox_id": "sandbox-12345",
  "command": "ls -la"
}'
```

#### 创建会话
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/create_session' \
-H 'Content-Type: application/json' \
-d '{
  "sandbox_id": "sandbox-12345",
  "session": "my_session"
}'
```

#### 在会话中执行命令
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/run_in_session' \
-H 'Content-Type: application/json' \
-d '{
  "sandbox_id": "sandbox-12345",
  "session": "my_session",
  "command": "python script.py"
}'
```

#### 上传文件
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/upload' \
-F 'file=@./local_file.txt' \
-F 'target_path=./remote_file.txt' \
-F 'sandbox_id=sandbox-12345'
```

#### 停止沙箱
```bash
curl -X POST 'http://localhost:8080/apis/envs/sandbox/v1/stop' \
-H 'Content-Type: application/json' \
-d '{
  "sandbox_id": "sandbox-12345"
}'
```

### 4.2 GEM API 示例

```bash
# 创建GEM环境
curl -X POST 'http://localhost:8080/apis/v1/envs/gem/make' \
-H 'Content-Type: application/json' \
-d '{"env_id": "game:Sokoban-v0-easy"}'

# 重置环境
curl -X POST 'http://localhost:8080/apis/v1/envs/gem/reset' \
-H 'Content-Type: application/json' \
-d '{"sandbox_id": "sandbox-12345", "seed": 42}'

# 执行步骤
curl -X POST 'http://localhost:8080/apis/v1/envs/gem/step' \
-H 'Content-Type: application/json' \
-d '{"sandbox_id": "sandbox-12345", "action": "random_action"}'

# 关闭环境
curl -X POST 'http://localhost:8080/apis/v1/envs/gem/close' \
-H 'Content-Type: application/json' \
-d '{"sandbox_id": "sandbox-12345"}'
```