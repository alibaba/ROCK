# Model Service（实验性）

ROCK 提供的 Model Service 负责处理 AI 模型调用的通信，为代理(Agent)和训练框架(如 Roll)或实际的 LLM 推理服务之间提供通信桥梁。

## 与 RockAgent 集成

ModelService 通常由 **RockAgent** 自动管理，无需手动调用生命周期方法。只需在配置中启用即可：

```python
from rock.sdk.sandbox.model_service.base import ModelServiceConfig

config = ModelServiceConfig(
    enabled=True,  # 启用 ModelService，RockAgent 会自动管理其生命周期
)
```

RockAgent 会自动：
- 安装 ModelService（安装 Python 运行时环境、安装模型服务包）
- 启动/停止 ModelService
- 监控 Agent 进程

## 架构概述（Local 模式）

Local 模式下，模型服务使用**文件系统**作为通信媒介，实现代理和模型间的请求-响应机制。

当 Agent 需要调用模型时，请求首先写入日志文件，然后由负责监听的组件处理响应。当模型生成响应后，结果将写回日志文件，并由等待的 Agent 读取。

## anti_call_llm - 核心 API

`anti_call_llm()` 是 **Local 模式**下最重要的 API，用于手动触发 LLM 反调用，实现模型调用的精细控制：

```python
result = await model_service.anti_call_llm(
    index=0,                                   # LLM 调用索引
    response_payload='OpenAI type response',       # 响应数据（可选）
    call_timeout=600,                          # 操作超时（秒）
    check_interval=3,                          # 状态检查间隔（秒）
)
```

**使用场景：**
- Agent 捕获到 LLM 响应后，调用此方法通知 Roll 运行时
- 支持携带响应数据，用于错误处理或重试
- 超时和检查间隔可配置，适应不同网络环境

## CLI 命令

如果需要通过 CLI 使用模型服务，ROCK 提供了一个 CLI 命令集，可以在沙箱中安装 ROCK 后，通过 `rock model-service` 访问：

### start 命令
开始模型服务进程
```bash
rock model-service start --type [local|proxy] [选项]
```

参数:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--type` | str | `local` | 服务类型：`local` 或 `proxy` |
| `--config-file` | str | None | 配置文件路径 |
| `--host` | str | None | 服务器地址（覆盖配置） |
| `--port` | int | None | 服务器端口（覆盖配置） |
| `--proxy-base-url` | str | None | 代理基础 URL |
| `--retryable-status-codes` | str | None | 可重试状态码，逗号分隔 |
| `--request-timeout` | int | None | 请求超时秒数 |

### watch-agent 命令
监控代理进程，当进程退出时发送 SESSION_END 消息
```bash
rock model-service watch-agent --pid <进程ID>
```

参数:
- `--pid`: 需要监控的代理进程 ID

### stop 命令
停止模型服务
```bash
rock model-service stop
```

### anti-call-llm 命令
反调用 LLM 接口
```bash
rock model-service anti-call-llm --index <索引> [--response <响应>]
```

参数:
- `--index`: 上一个 LLM 调用的索引，从 0 开始
- `--response`: 上一次 LLM 调用的响应（可选）

## 文件通信协议

模型服务使用文件进行进程间通信，定义了特定的标记格式用于区分请求和响应：

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

## SDK 使用

### ModelServiceConfig

模型服务配置类，位于 `rock/sdk/sandbox/model_service/base.py`：

```python
from rock.sdk.sandbox.model_service.base import ModelServiceConfig

config = ModelServiceConfig(
    enabled=True,
    type="local",                                      # 服务类型
    install_cmd="pip install rock-model-service",      # 安装命令
    install_timeout=300,                               # 安装超时（秒）
    start_cmd="rock model-service start --type ${type}",  # 启动命令
    stop_cmd="rock model-service stop",                # 停止命令
    logging_path="/data/logs",                         # 日志路径
    logging_file_name="model_service.log",             # 日志文件名
)
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enabled` | `False` | 是否启用模型服务（RockAgent 自动管理） |
| `type` | `"local"` | 服务类型：`local` 或 `proxy` |
| `install_cmd` | - | 模型服务包安装命令 |
| `install_timeout` | `300` | 安装超时时间（秒） |
| `start_cmd` | - | 启动命令模板 |
| `stop_cmd` | - | 停止命令 |
| `logging_path` | `/data/logs` | 日志目录路径 |
| `logging_file_name` | `model_service.log` | 日志文件名 |

### ModelService

模型服务管理类，处理沙箱内模型服务的生命周期：

```python
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.model_service.base import ModelServiceConfig, ModelService

sandbox = Sandbox(config)
model_service = ModelService(sandbox, ModelServiceConfig())

# 通常由 RockAgent 自动管理，无需手动调用
# 以下方法仅在需要手动控制时使用

# 安装模型服务
await model_service.install()

# 启动模型服务
await model_service.start()

# 监控代理进程
await model_service.watch_agent(pid="12345")

# 执行反调用 LLM（Local 模式核心 API）
result = await model_service.anti_call_llm(
    index=0,
    response_payload='{"content": "response"}',
    call_timeout=600,
    check_interval=3,
)

# 停止模型服务
await model_service.stop()
```

## API 参考

### install()

在沙箱中安装模型服务依赖。

```python
await model_service.install()
```

执行步骤：
1. 创建并初始化 Python 运行时环境
2. 创建 Rock 配置文件
3. 安装模型服务包

**注意：** 通常由 RockAgent 自动调用。

### start()

启动模型服务。

```python
await model_service.start()
```

前提条件：必须先调用 `install()`。

**注意：** 通常由 RockAgent 自动调用。

### stop()

停止模型服务。

```python
await model_service.stop()
```

如果服务未运行，会跳过此操作。

**注意：** 通常由 RockAgent 自动调用。

### watch_agent(pid)

监控代理进程。

```python
await model_service.watch_agent(pid="12345")
```

当进程退出时，发送 `SESSION_END` 消息。

### anti_call_llm(index, response_payload, call_timeout, check_interval)

执行反调用 LLM 操作。**这是 Local 模式下最重要的 API。**

```python
result = await model_service.anti_call_llm(
    index=0,                                   # LLM 调用索引
    response_payload='{"result": "..."}',       # 响应数据（可选）
    call_timeout=600,                          # 操作超时（秒）
    check_interval=3,                          # 状态检查间隔（秒）
)
```

## 配置选项

### 服务配置
- `SERVICE_HOST`: 服务主机地址，默认为 `"0.0.0.0"`
- `SERVICE_PORT`: 服务端口，默认为 `8080`

### 日志配置
- `LOG_FILE`: 用以通信的日志文件路径，包含请求和响应数据

### 轨迹（Traj）日志记录
模型服务将 LLM 调用轨迹（traj）记录到 JSONL 文件中，用于调试和分析。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `ROCK_MODEL_SERVICE_DATA_DIR` | `/data/logs` | traj 日志文件目录 |
| `ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE` | `false` | 追加模式（true/false） |

**traj 文件位置**: `{DATA_DIR}/LLMTraj.jsonl`

**traj 文件格式**（JSONL - 每行一个 JSON 对象）：
```json
{"request": {...}, "response": {...}}
```

### 轮询配置
- `POLLING_INTERVAL_SECONDS`: 轮询间隔，默认为 `0.1` 秒
- `REQUEST_TIMEOUT`: 请求超时时间，默认为无限

### 标记配置
定义了用于区分日志文件中不同类型消息的标记：
- `REQUEST_START_MARKER` / `REQUEST_END_MARKER`
- `RESPONSE_START_MARKER` / `RESPONSE_END_MARKER`
- `SESSION_END_MARKER`

### ModelServiceConfig（服务端）

服务端配置类定义了模型服务如何处理请求：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `host` | str | `"0.0.0.0"` | 服务器地址 |
| `port` | int | `8080` | 服务器端口 |
| `proxy_base_url` | str \| None | `None` | 直接代理 URL |
| `proxy_rules` | dict | 见下方 | 模型名称到 URL 的映射 |
| `retryable_status_codes` | list[int] | `[429, 500]` | 可重试的 HTTP 状态码 |
| `request_timeout` | int | `120` | 请求超时时间（秒） |

**默认 proxy_rules**:
```python
{
    "gpt-3.5-turbo": "https://api.openai.com/v1",
    "default": "https://api-inference.modelscope.cn/v1",
}
```
