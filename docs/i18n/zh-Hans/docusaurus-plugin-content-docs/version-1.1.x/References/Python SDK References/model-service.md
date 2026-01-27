---
sidebar_position: 2
---

# 模型服务 (Model Service)（实验性）

ROCK提供的Model Service负责处理AI模型调用的通信，为代理(Agent)和训练框架(如Roll)或实际的LLM推理服务之间提供通信桥梁。

## 架构概述

模型服务使用文件系统作为通信媒介，实现代理和模型间的请求-响应机制。当代理需要调用模型时，请求首先写入日志文件，然后由负责监听的组件处理响应。当模型生成响应后，结果将写回日志文件，并由等待的代理读取。

## CLI命令

如果需要通过CLI使用模型服务, ROCK提供了一个CLI命令集，可以在沙箱中安装ROCK后, 通过 `rock model-service` 访问:

### start命令
开始模型服务进程
```bash
rock model-service start --type [local|proxy]
```

参数:
- `--type`: 模型服务类型，可选`local`或`proxy`，默认为`local`

### watch-agent命令
监控代理进程，当进程退出时发送 SESSION_END 消息
```bash
rock model-service watch-agent --pid <进程ID>
```

参数:
- `--pid`: 需要监控的代理进程ID

### stop命令
停止模型服务
```bash
rock model-service stop
```

### anti-call-llm命令
反调用LLM接口
```bash
rock model-service anti-call-llm --index <索引> [--response <响应>]
```

参数:
- `--index`: 上一个LLM调用的索引，从0开始
- `--response`: 上一次LLM调用的响应（可选）

## 文件通信协议

模型服务使用文件进行进程间通信，定义了特定的标记格式用于区分请求和响应:

### 请求格式
```
LLM_REQUEST_START{JSON请求数据}LLM_REQUEST_END{元数据JSON}
```

### 响应格式
```
LLM_RESPONSE_START{JSON响应数据}LLM_RESPONSE_END{元数据JSON}
```

### 会话结束标识
```
SESSION_END
```

元数据包含时间戳和索引信息，用于保证消息顺序和处理。

## 沙箱集成

### ModelServiceConfig
位于`rock/sdk/sandbox/model_service/base.py`，定义了沙箱中的模型服务配置:
- 工作目录
- Python和模型服务安装命令
- 会话环境变量
- 各种命令模板

### ModelService类
处理沙箱内模型服务的生命周期:
- `install()`: 在沙箱中安装模型服务依赖
- `start()`: 启动模型服务
- `stop()`: 停止模型服务
- `watch_agent()`: 监控代理进程
- `anti_call_llm()`: 执行反调用LLM操作

## 工作流程

1. 代理发起模型调用请求
2. 请求被格式化并写入日志文件
3. 模型服务监听日志文件，捕获新请求
4. 运行时(Roll)处理请求并生成响应
5. 响应写入日志文件
6. 模型服务返回响应给代理

## 配置选项

### 服务配置
- `SERVICE_HOST`: 服务主机地址，默认为"0.0.0.0"
- `SERVICE_PORT`: 服务端口，默认为8080

### 日志配置
- `LOG_FILE`: 用以通信的日志文件路径，包含请求和响应数据

### 轮询配置
- `POLLING_INTERVAL_SECONDS`: 轮询间隔，默认为0.1秒
- `REQUEST_TIMEOUT`: 请求超时时间，默认为无限

### 标记配置
定义了用于区分日志文件中不同类型消息的标记:
- `REQUEST_START_MARKER` / `REQUEST_END_MARKER`
- `RESPONSE_START_MARKER` / `RESPONSE_END_MARKER`
- `SESSION_END_MARKER`