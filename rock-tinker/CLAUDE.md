# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Rock Tinker 是 [ROCK/Tinker](https://thinkingmachines.ai/tinker) 的官方 Python SDK，用于 LLM 微调训练和推理采样。SDK 基于 Python 3.11+，使用 `uv` 管理依赖和构建。

## 常用命令

```bash
# 安装依赖（开发模式）
uv sync

# 运行全部测试（并行）
uv run pytest

# 运行单个测试文件
uv run pytest tests/test_tinker_client_protocol.py

# 运行单个测试用例
uv run pytest tests/test_tinker_client_protocol.py::test_tinker_client_passes_project_id_on_session_create

# 类型检查
uv run pyright
uv run mypy src/

# Lint / 格式化
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

## 架构概览

### 用户侧入口：三个高层 Client

```
tinker.TinkerClient        # 会话入口：创建训练/采样/REST client
tinker.TrainingClient       # 训练：forward_backward(), optim_step()
tinker.SamplingClient       # 推理采样：sample()
tinker.RestClient           # REST 操作：list_checkpoints(), get_training_run()
tinker.APIFuture            # 异步结果句柄，支持 .result() 和 await
```

这四个类定义在 `src/tinker/lib/public_interfaces/`，通过 `src/tinker/__init__.py` 直接导出。

### 分层架构

```
public_interfaces/          # 用户直接使用的 API 层
    tinker_client.py        # TinkerClient：会话管理和 client 工厂
    training_client.py      # 训练循环：fwdbwd, optim_step, save_weights
    sampling_client.py      # 采样：sample()，支持 subprocess_sampling 模式
    rest_client.py          # REST 查询：checkpoints, runs, publish
    api_future.py           # APIFuture 抽象基类

lib/                        # 内部实现层
    internal_client_holder.py   # 异步 HTTP 连接池（基于 AsyncTinker）
    api_future_impl.py          # APIFuture 具体实现，含重试逻辑
    retry_handler.py            # RetryConfig + RetryHandler（指数退避）
    sidecar.py                  # subprocess sidecar，GIL 隔离 RPC 执行
    chunked_fwdbwd_helpers.py   # 大 batch 分块 forward_backward 辅助
    telemetry.py                # 遥测批量上报
    _auth_token_provider.py     # API Key / JWT 认证策略

_client.py                  # AsyncTinker（低层 httpx async HTTP client）
resources/                  # 低层 REST resource（training/sampling/models 等）
types/                      # Pydantic 模型，覆盖所有请求/响应类型
proto/                      # Protobuf 定义（tinker_public_pb2）
```

### APIFuture 并发模型

所有耗时操作（forward_backward、optim_step、sample 等）立即返回 `APIFuture`，内部在后台线程的 asyncio 事件循环上异步执行。调用者可以：
- `future.result()` —— 阻塞直到完成
- `await future` 或 `await future.result_async()` —— 在 async 上下文中等待

`InternalClientHolder` 维护一个独立线程持有的 asyncio 循环，以及一个 `ClientConnectionPool`（多个 `AsyncTinker` httpx client），并行 HTTP 请求时通过引用计数路由到空闲 client。

### Sidecar 子进程采样

`SamplingClient` 支持 `subprocess_sampling=True`（或环境变量 `TINKER_SUBPROCESS_SAMPLING=1`），在独立子进程里运行采样，避免 PyTorch GIL 阻塞训练线程。机制：
- `create_sidecar_handle(target)` 启动子进程，通过 `multiprocessing.Pipe` 传递 picklable RPC 对象
- 每个 RPC 继承 `SidecarRPC` 并实现 `async def execute(self, target)`

### CLI（`tinker` 命令）

CLI 入口在 `src/tinker/cli/`，使用 Click + 自定义 `LazyGroup` 实现懒加载（保证 `tinker --help` <50ms 启动）。命令组织：

```
tinker version
tinker run list / info <run-id>
tinker checkpoint list [run-id] / info <ckpt-id> / push-hf <path>
```

CLI 设计规范详见 `src/tinker/cli/AGENTS.md`。所有 CLI 错误须抛出 `TinkerCliError`，不得直接 `sys.exit()`。

### tinker_cookbook/

独立的配套工具库（不属于 SDK 核心包），包含：
- `renderers/`：各模型的 prompt 构建/解析器（Qwen、LLaMA、DeepSeek、Kimi 等），用于将对话转换为 token 序列
- `completers.py`：采样完成器
- `rl/`：强化学习 utilities
- `eval.py`：评估工具

## 环境变量

| 变量 | 用途 |
|------|------|
| `TINKER_API_KEY` | API 鉴权密钥（必需） |
| `TINKER_BASE_URL` | 覆盖默认 endpoint（默认：`https://tinker.thinkingmachines.dev/services/tinker-prod`） |
| `TINKER_SUBPROCESS_SAMPLING` | 设为 `1`/`true` 启用子进程采样隔离 |

## 测试

测试使用 `respx` mock HTTP 请求，`pytest-asyncio` 处理异步测试（`asyncio_mode = "auto"`），`pytest-xdist` 并行执行。测试配置在 `pyproject.toml` 的 `[tool.pytest.ini_options]`。

`tinker_cookbook/renderers/` 里的测试关注 token 级别属性：`build_generation_prompt`、`build_supervised_example`、`parse_response` 三者之间的对应关系，以及 sequence extension 不变式。
