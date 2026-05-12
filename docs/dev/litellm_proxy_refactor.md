# LiteLLM 重构 model-service proxy + 加 record/replay —— Handoff 文档

> 这份文档是给"接手者"(可能是另一个 Claude session 或人)看的,目的是让接手者**完全不看上一段对话**也能从我离开的地方继续往下做。文档放在 `docs/dev/litellm_proxy_refactor.md`。

---

## 0. TL;DR

**目标**:把 `rock model-service --type proxy` 的自写 httpx forward + retry 替换为基于 `litellm` SDK 的实现;同时把 chat/completions 轨迹的"录制 + 顺序回放"作为一等公民能力做进来,服务 SWE-agent / mini-swe-agent / OpenHands 类 deterministic agent 的"无 LLM 成本"调试。

**当前状态**:**代码改动、单元测试、lint 全部完成通过**。下一步是集成验证(实际起 proxy + curl)和写 PR。

**完成清单**:
- ✅ `pyproject.toml` `model-service` extras 加 `litellm>=1.50.0`
- ✅ `ModelServiceConfig` 加 `traj_enabled / traj_file / traj_append / replay_enabled / replay_traj_path / num_retries` 6 个字段
- ✅ 新模块 `rock/sdk/model/server/integrations/{__init__.py, traj_recorder.py, traj_replayer.py}`
- ✅ `rock/sdk/model/server/api/proxy.py` 整文件重写为 litellm SDK 调用
- ✅ `rock/sdk/model/server/main.py` 加 `_configure_litellm_for_proxy()` + 新 CLI flags(`--num-retries / --traj-file / --no-traj / --replay-traj`)
- ✅ `rock/sdk/model/server/utils.py` 保留 `record_traj` 装饰器(给 local 模式继续用),proxy 模式不再用
- ✅ `tests/unit/sdk/model/test_proxy.py` 改造完成(把 `patch perform_llm_request` 改为 `patch litellm.acompletion`)
- ✅ 新测试 `tests/unit/sdk/model/test_traj_recorder.py` + `test_traj_replayer.py`
- ✅ `examples/model_service/config_record.yaml` + `config_replay.yaml`
- ✅ **单元测试全部通过**(`uv run pytest tests/unit/sdk/model/` → 47 passed)
- ✅ **Lint/format 全部干净**(`ruff check` + `ruff format --check`，修了一个 `Optional[str]` → `str | None` 的 UP045)

**未完成 / 阻塞**:
- ⏳ **集成验证**(实际起 proxy + curl + agent 端到端,见第 4.4 节)
- ⏳ **PR 描述里的 breaking change 提示**(见第 5 节)

**原始 plan 文件**(更详细的设计推演):`/home/xinshi/.claude/plans/litellm-chat-completions-traj-replay-ser-lucky-rainbow.md`(在主 Claude 配置目录,不在 rock 仓内)。

---

## 1. 背景与目标

### 起因

用户问:"litellm 能支持把 chat/completions 接口的轨迹落盘吗,然后我想看看能否支持根据 traj 文件做一个 replay server, 比如给一些其他的 agent (swe-agent, openhands) 等用来做 traj 回放"。

### 需求方向的几次迭代(避免接手者重走弯路)

1. **第一版方向**:做一个独立 Python 项目 `litellm-traj`,里面定义 `CustomLogger` 子类(record)和 `CustomLLM` 子类(replay),通过 dotted-path 注册到 litellm proxy 的 `config.yaml`。**已废弃**。
2. **第二版方向**:在 rock 仓内把这个能力做进 `rock/sdk/model/server/api/proxy.py`(rock 已有 model-service)。但用户进一步要求:**重构掉 rock 自写的 proxy 实现,改为基于 litellm**。
3. **最终方向(本次)**:用 **litellm SDK** 替换 `proxy.py` 内手写的 httpx forward + `retry_async`;record 接 `CustomLogger`,replay 接 `CustomLLM` provider。`rock model-service` CLI、`local` 模式、FastAPI app/health/metrics 全部保留不动 —— 只动 proxy 模式。

### 为什么是 litellm SDK 而不是 litellm proxy

我们已经有 rock 自己的 FastAPI app + CLI + auth/metrics middleware,只需要一个"OpenAI 兼容上游调用 + 错误归一化 + 流式聚合 + record/replay 接入点"。**litellm SDK 是这层能力的最小外加**,不需要把 litellm proxy 整套生命周期/配置体系拽进来。litellm proxy 适合"完全没有 server 的人"用,我们已经有 server。

### 用户最终拍板的 4 个关键设计选择

| 维度 | 选择 | 理由 |
|---|---|---|
| 集成模式 | **litellm SDK** | 改动面最小,保留 rock 既有 FastAPI/CLI/metrics |
| traj schema | **`StandardLoggingPayload`(litellm 原生)** | 字段最全(messages/response/usage/timing/error_information/trace_id),与 litellm 生态互通 |
| 是否本期做 replay | **是,record + replay 一起** | 用户原始诉求就是回放;基础设施一次性铺好 |
| 流式 | **顺便解禁** | litellm 自动聚合,record/replay 走流式不增加复杂度 |

---

## 2. 改动清单(按文件)

### 2.1 `pyproject.toml` —— 修改

`[project.optional-dependencies]` 的 `model-service` 数组追加一项 `"litellm>=1.50.0"`。其它 extras 不动。

```toml
model-service = [
    "fastapi",
    "uvicorn",
    "psutil",
    "swebench",
    "alibabacloud_cr20181201==2.0.5",
    "litellm>=1.50.0",   # ← 这一行新加
]
```

为什么是 `>=1.50.0`:这个版本之后 `StandardLoggingPayload`、`CustomLogger.async_log_success_event` 接口、`async_mock_completion_streaming_obj` 都已稳定。本仓现有 model-service 测试集没装过 litellm,所以全新引入,不存在升级冲突。

### 2.2 `rock/sdk/model/server/config.py` —— 修改

在 `ModelServiceConfig` 末尾新增 6 个字段(注意顺序、类型、默认值):

```python
num_retries: int = Field(default=6)

traj_enabled: bool = Field(default=True)
traj_file: str | None = Field(default=None)
traj_append: bool = Field(default=True)   # 注意:旧默认是 False(覆盖),这里翻成 True

replay_enabled: bool = Field(default=False)
replay_traj_path: str | None = Field(default=None)
```

每个字段的语义和取值范围都写在 docstring 里。`traj_append=True` 是这次的**默认行为变更**(旧的 `_write_traj` 默认覆盖,被认为是 bug)。`TRAJ_FILE`、`LOG_FILE`、`LOG_DIR` 模块级常量保留不动。

### 2.3 `rock/sdk/model/server/integrations/__init__.py` —— 新增(空文件)

只为了让 `integrations` 成为一个包,内容为空。

### 2.4 `rock/sdk/model/server/integrations/traj_recorder.py` —— 新增

`TrajectoryRecorder(CustomLogger)`,实现两个钩子:`async_log_success_event` 和 `async_log_failure_event`。每次调用从 `kwargs["standard_logging_object"]` 取出 `StandardLoggingPayload`(dict 形态),append 一行 JSON 到 `traj_file`,同时上报 OTLP `model_service.request.{rt,count}` metrics。

关键设计点(展开见第 3.1 节):
- streaming 不分支(litellm 已在 callback 触发前把 chunks 聚合写入 `payload.response`)
- `asyncio.Lock` per recorder + `asyncio.to_thread` 包同步写,避免在 event loop 阻塞
- `append=False` 模式只在**首次写**时截断(避免每次调用覆盖)
- metrics 复用 `rock.sdk.model.server.utils._get_or_create_metrics_monitor`,`MODEL_SERVICE_REQUEST_RT/COUNT` 常量

### 2.5 `rock/sdk/model/server/integrations/traj_replayer.py` —— 新增

包含两个类 + 两个 helper:

- `SequentialCursor`:从 jsonl 文件或目录加载 records,`async next()` 返回下一条并推进游标,越界 raise `CustomLLMError(404)`。带 `asyncio.Lock` 防并发推进。`reset()` 用于回到起点。
- `_record_to_model_response(record)` / `_extract_assistant_text(record)`:把 record 还原成 `litellm.types.utils.ModelResponse` 或抽出 assistant text(给 streaming 用)。
- `TrajectoryReplayer(CustomLLM)`:实现 `acompletion` 和 `astreaming`。流式拆分直接调 `litellm.utils.async_mock_completion_streaming_obj`,不自己造轮子。

`acompletion`/`astreaming` 的签名是 `(self, model, messages, *args, **kwargs)`。litellm 调 CustomLLM 时**全部用关键字参数**(litellm/main.py:4302-4319 实测),所以 `kwargs.get("model_response")` 能可靠拿到流式拆分需要的目标对象。

### 2.6 `rock/sdk/model/server/utils.py` —— 修改(保留 + 注释更新)

**关键决定**:不删 `record_traj` / `_write_traj`。原因:`local.py` 仍在用 `@record_traj`,plan 阶段说过"local 模式不动";所以 record_traj 保留,docstring 加一段说明"proxy 不再用,只给 local 用",新引导走 `TrajectoryRecorder`。

`_get_or_create_metrics_monitor` / `MODEL_SERVICE_REQUEST_RT` / `MODEL_SERVICE_REQUEST_COUNT` 不动 —— `traj_recorder.py` 复用之。

### 2.7 `rock/sdk/model/server/api/proxy.py` —— 整文件重写

旧实现:
- `httpx.AsyncClient` 全局 + `@retry_async` 6 次指数退避
- `perform_llm_request(url, body, headers, config)` 自管 retry
- `@record_traj` 挂在 handler 上同步落盘 + metrics
- 强制 `stream=False`(MVP 限制)

新实现:
- `litellm.acompletion(model, api_base, extra_headers, timeout, num_retries, **body)`
- 错误归一化:catch `RateLimitError / APIError / BadRequestError / AuthenticationError / Timeout` → `_format_error_response()` 回退到 `{error:{message,type,code}}` schema(agent 端关键字检测兼容)
- 流式开放:`stream=True` 走 `StreamingResponse(_sse_iter(...))`
- 不再有装饰器 —— record 落盘改由 `main.py` 在启动时挂的 `litellm.callbacks` 完成

`get_base_url()` 路由优先级**完全保留**(`proxy_base_url` > `proxy_rules[model]` > `proxy_rules["default"]`)。`_filter_headers()` 把 hop-by-hop headers(host/content-length/content-type/transfer-encoding/connection)滤掉,Authorization 等保留。

replay 模式下:`litellm_model = f"traj-replay/{model_name}"`,`api_base=None`。litellm 看到 `traj-replay/` 前缀会查 `litellm.custom_provider_map`,找到 `TrajectoryReplayer` 实例并调它的 `acompletion`/`astreaming`。

### 2.8 `rock/sdk/model/server/main.py` —— 修改

新增私有函数 `_configure_litellm_for_proxy(config)`,在 `main()` 进入 proxy 分支时(`include_router(proxy_router)` 之前)调用一次。两个分支:

```python
if config.replay_enabled:
    # 注册 TrajectoryReplayer 到 litellm.custom_provider_map
    ...
elif config.traj_enabled:
    # 把 TrajectoryRecorder 加到 litellm.callbacks
    ...
```

**注意**:replay 和 record 互斥(replay 不要再录,否则录回放结果会污染 source-of-truth)。

`create_config_from_args()` 新增 4 个 CLI override:`--num-retries / --traj-file / --no-traj / --replay-traj`。所有用 `getattr(args, "<name>", default)` 的方式取,这样老的调用方(传不带这些字段的 Namespace)不会炸。

`from rock.sdk.model.server.config import TRAJ_FILE, ModelServiceConfig` —— 新增 `TRAJ_FILE` 导入,因为 `_configure_litellm_for_proxy` 在 `traj_file` 未指定时回退到 `TRAJ_FILE`。

### 2.9 `tests/unit/sdk/model/test_proxy.py` —— 重写

- 删除:`test_perform_llm_request_*`(4 个,perform_llm_request 已不存在)
- 改造:`test_chat_completions_routing_*`、`test_proxy_base_url_overrides_proxy_rules` —— `patch_path` 从 `proxy.perform_llm_request` 改为 `proxy.litellm.acompletion`
- 改造:断言从"perform_llm_request 第一个位置参数 == URL"改为"litellm.acompletion kwargs 中 `api_base == 期望值`,`model == 'openai/<name>'`"
- 新增:`test_chat_completions_passes_num_retries_and_timeout` / `test_chat_completions_litellm_error_returns_proxy_schema` / `test_chat_completions_replay_mode_uses_traj_replay_provider` / `test_chat_completions_strips_hop_by_hop_headers` / `test_config_default_traj_and_replay` / `test_config_loads_traj_and_replay_from_file` / `test_cli_replay_traj_enables_replay`
- 保留:所有 lifespan / config-load / metrics-monitor / record_traj 测试(record_traj 在 utils.py 还在,给 local 用)

mock 返回的 ModelResponse:用 `SimpleNamespace(model_dump=lambda: payload)` 假装一个 pydantic 对象 —— 因为 handler 只调 `.model_dump()`,不需要真 import 整个 ModelResponse。

### 2.10 `tests/unit/sdk/model/test_traj_recorder.py` —— 新增

7 个测试:JSONL append / `append=False` 首次截断 / metrics + sandbox_id / failure 落盘 / 缺 standard_logging_object 跳过 / 自动建父目录 / `response_time` 缺失时回退到 `endTime - startTime`。

mock 思路:`patch("rock.sdk.model.server.integrations.traj_recorder._get_or_create_metrics_monitor", return_value=mock_monitor)` —— recorder 内部 import 了这个函数,mock 它的引用。

### 2.11 `tests/unit/sdk/model/test_traj_replayer.py` —— 新增

11 个测试:cursor 加载单文件/目录(按文件名 sort)/空行/缺失文件 raise / `next()` 顺序返回 / 越界 raise / `reset()` 回到起点 / model mismatch 只 warn / Replayer.acompletion 命中 record / cursor 推进 / streaming chunk 拼回 == 原文 / 越界 raise CustomLLMError。

streaming 测试构造一个 `SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(role=None, content=None), index=0)])` 当 model_response,因为 `async_mock_completion_streaming_obj` 内部会写 `model_response.choices[0].delta.content = ...`。

### 2.12 `examples/model_service/config_record.yaml` 和 `config_replay.yaml` —— 新增

两份开箱即用的 yaml,带详细注释。`config_record.yaml` 默认开 `traj_enabled: true / traj_append: true`,关 replay。`config_replay.yaml` 默认关 traj_enabled / 开 replay,`replay_traj_path: "/data/logs/LLMTraj.jsonl"` 占位 —— 实际部署时根据 traj 位置改。

### 2.13 `/mnt/xinshi/github/litellm-traj/` —— 已删除

第一版独立项目骨架(`pyproject.toml / src/litellm_traj/cursor.py / .gitignore / LICENSE`)在方向变更时已 `rm -rf`。所有有效内容都迁回了 rock 的 integrations/ 模块。

---

## 3. 关键代码细节(踩坑点 + "为什么这么写")

下文展开几个最容易让接手者迷失的设计选择。每一项都标了 litellm 仓内的源码定位(litellm 主仓在 `/mnt/xinshi/github/litellm/`),便于交叉验证。

### 3.1 Streaming 聚合在 litellm 内部完成,Recorder 不需要分支

`StandardLoggingPayload.response` 字段在 `success_handler` 触发前**已经是聚合完整的 OpenAI shape dict**。流式与非流式走同一条路径:litellm 在 streaming 结束时调用 `stream_chunk_builder` 拼出 `complete_streaming_response`(litellm 仓 `litellm/litellm_core_utils/litellm_logging.py:1930-1955`),然后写入 `standard_logging_object.response`。

实际后果:`TrajectoryRecorder.async_log_success_event` 拿到的 payload 永远含完整 response,我**不需要写 `async_log_stream_event`**。这也是为什么 stream 解禁几乎"零成本" —— 录制端无任何额外代码。

### 3.2 `model: "openai/<name>"` 前缀的含义

litellm 把"provider"前缀作为路由依据。`openai/gpt-3.5-turbo` 表示"上游是 OpenAI 兼容协议的服务,模型名叫 gpt-3.5-turbo"。配合 `api_base="https://api.modelscope.cn/v1"` 这种第三方 OpenAI 兼容 endpoint 也能用 —— 这正是 rock 现有 `proxy_rules` 里的 ModelScope/OpenAI 等场景。

`traj-replay/<name>` 是我们注册的自定义 provider。litellm 看到这个前缀会查 `litellm.custom_provider_map`,匹配到 `provider == "traj-replay"` 的项,把 `custom_handler.acompletion`/`astreaming` 当上游调(litellm 仓 `litellm/main.py:4280-4326`)。

### 3.3 错误归一化:为什么 catch 那 5 个 exception

`proxy.py` catch 顺序:`RateLimitError, APIError, BadRequestError, AuthenticationError, Timeout`。这五个在 `litellm/exceptions.py` 全部继承自 `openai.OpenAIError` 派生类,**都带 `.status_code` 属性**。`_format_error_response` 用 `getattr(exc, "status_code", None) or 502` 提取上游真实状态码;message 走 `str(exc)` —— litellm 异常的 `__str__` 已经包含"上游原始 error message",所以 agent 端的关键字检测(如 `"context length exceeded"` / `"content violation"`)继续工作。

`type` 字段用 `type(exc).__name__`(`"BadRequestError"` 等),不再是旧的固定 `"proxy_retry_failed"`。这是 schema 的语义变化:同一个 `error.type` 字段,旧版本返回固定字符串,新版本返回 exception 类名。如果有下游消费 `error.type` 做分支,需要适配。

兜底 `except Exception` 走 `HTTPException(500)`,会被 `main.py` 里的 `global_exception_handler` 接住,返回 `{error:{message,type:"internal_error",code:"internal_error"}}` —— 这条路径与重构前完全一致。

### 3.4 retry 行为:从 `retry_async` 切到 `litellm.num_retries`

旧实现:`@retry_async(max_attempts=6, delay_seconds=2.0, backoff=2.0, jitter=True, exceptions=(TimeoutException, ConnectError, HTTPStatusError))`。仅在 `status_code in retryable_status_codes` 时 raise,这样 401 不会触发 retry,而 429/500 会。

新实现:`config.num_retries`(默认 6) 直接传给 `litellm.acompletion(num_retries=...)`。litellm 内部对 `RateLimitError / APIError / Timeout / ServiceUnavailableError` 自动重试,**不暴露 `retryable_status_codes` 维度**。我保留 `retryable_status_codes` 字段在 config 里,但当前**handler 没用它**(向后兼容旧 yaml,不会因为多了字段而 reject)。

如果将来有人投诉"自定义重试码列表失效",这是已知的语义差异。fallback 方案:在 handler 里手写 `for attempt in range(config.num_retries):` 包一层,根据 status code 做白名单。本期不做,因为 litellm 默认行为已经覆盖最常见的 429/500。

### 3.5 `_filter_headers` 黑名单 vs 白名单

我用黑名单:`host / content-length / content-type / transfer-encoding / connection` 不转发,其余全部透传给 litellm 的 `extra_headers`。这与旧实现保持一致(旧的也是去掉前 4 个,新增 connection 是为了更标准)。Authorization/X-* 等都自动通过。

注意:`extra_headers` 在 litellm 里被合并到上游 HTTP 请求里(litellm 自己的 OpenAI client),不会覆盖 litellm 自己生成的 `Authorization: Bearer <api_key>`。如果 rock 不主动设 `OPENAI_API_KEY`,而 client 又传了 Authorization header,litellm 会用 client 的;反之 litellm 会用环境变量。这一层逻辑全在 litellm 自己。

### 3.6 `traj_append=False` 的"首次截断"行为

旧 `_write_traj` 在 `append=False` 时**每次调用都 `mode="w"`**,导致 jsonl 永远只有最后一行 —— 这是个 bug。

新 `TrajectoryRecorder` 的修复:维护一个 `self._truncated` 实例标志;`append=False` 时,**第一次写**用 `mode="w"`(覆盖上一进程留下的旧 traj),**后续写**用 `mode="a"`(本进程内 append)。所以:
- 进程启动时:旧 traj 文件清空(如果存在)
- 进程运行中:每次调用 append 一行
- 进程重启:再次清空,从头记

效果上等于"per-run 一份完整 traj"。我把这个语义在 docstring 里讲清楚了,因为这是和旧默认行为最不同的一点。

`traj_append=True`(新默认)就是纯 append-only,不管旧文件。

### 3.7 SequentialCursor 的并发模型

`async next()` 用 `asyncio.Lock` 保护索引 + 自增。**单进程多并发请求场景下** cursor 推进是原子的,但**含义是"按到达顺序消费"**,所以多个 agent 并发打过来会被串成一个伪顺序 —— 这是 v1 的已知约束(plan 里明确列出),约定"单 agent 串行回放"。

**model mismatch 只 warn 不 raise**:expected_model 来自调用方传入,recorded model 来自 record 内的 `model` 字段。两者不一致只打 warning,record 仍然返回。理由:agent 端可能切换了 base_url 但没改 model 名(常见调试场景),不该硬阻塞。

### 3.8 CustomLLM 的调用约定 —— `*args, **kwargs` 收尾很重要

`litellm/main.py:4302-4319` 实测调用方式是**全关键字参数**:
```python
response = handler_fn(
    model=model, messages=messages, headers=headers,
    model_response=model_response, print_verbose=...,
    api_key=..., api_base=..., acompletion=..., logging_obj=...,
    optional_params=..., litellm_params=..., logger_fn=...,
    timeout=..., custom_prompt_dict=..., client=..., encoding=...,
)
```
但 litellm 各小版本会不会增减字段不确定。`TrajectoryReplayer.acompletion(self, model, messages, *args, **kwargs)` 这种"显式 model+messages,其余吞掉"的签名,既能 PEP-484 注解,又对 litellm 后续加字段免疫。

**不要改成 `def acompletion(self, model, messages, *, optional_params, ...)`** 否则 litellm 加新字段时会 TypeError。

### 3.9 `LITELLM_TRAJ_FILE` env vs `traj_file` 字段

我没引入新 env var。`config.traj_file` 在 `main.py:_configure_litellm_for_proxy` 里通过 `config.traj_file or TRAJ_FILE` 取值,而 `TRAJ_FILE` 来自 `config.py:13`,= `LOG_DIR + "/LLMTraj.jsonl"`,`LOG_DIR = env_vars.ROCK_MODEL_SERVICE_DATA_DIR`(默认 `/data/logs`)。

所以路径优先级:`--traj-file CLI` > `traj_file: yaml` > `LOG_DIR/LLMTraj.jsonl`(LOG_DIR 受 `ROCK_MODEL_SERVICE_DATA_DIR` env 控制)。和旧体系一致。

### 3.10 `record_traj` 装饰器为什么保留

`local.py:75` 仍然用 `@record_traj` 装饰它的 chat_completions handler。local 模式不调 litellm,FileHandler 直接通过文件 marker 跟 Roll 通信 —— 没有 litellm callback 触发的窗口。所以为了保留 local 模式的"调用次数 + RT 上报",我把 `record_traj` 留在 `utils.py`,让 local 继续用,docstring 写明"proxy 模式不再用,改走 TrajectoryRecorder"。

代价:local 模式录的 traj schema 是旧的 `{request, response}`,proxy 模式是 `StandardLoggingPayload`。两种 schema 共存于同一个 `LLMTraj.jsonl` 文件路径上(因为 `TRAJ_FILE` 是同一个常量)。**实际部署时 local 和 proxy 用同一个进程的概率为 0**(`--type` 互斥),所以同一个 traj 文件不会混合两种 schema。但如果有人定时切换 `--type` 跑 + `traj_append=true` 不轮换文件,会出现混合。文档建议:**replay 时只读 proxy 模式录的 traj**(StandardLoggingPayload 格式),local 模式的 traj 仅用于 local 调试。

---

## 4. 跑测试 / 验证步骤(接手者从这里继续)

### 4.1 准备 Python 环境

**已验证**:`uv sync` 后 litellm 已正常安装。使用 `uv run` 执行,不需要手动激活 venv。

```bash
cd /mnt/xinshi/github/Self-ROCK
uv sync --extra model-service --group test
```

验证依赖(已通过):

```bash
uv run python -c "from litellm.integrations.custom_logger import CustomLogger; print('ok')"
uv run python -c "from litellm.llms.custom_llm import CustomLLM, CustomLLMError; print('ok')"
uv run python -c "from litellm.utils import async_mock_completion_streaming_obj; print('ok')"
```

### 4.2 静态检查 / lint

```bash
uv run ruff check rock/sdk/model/server/ tests/unit/sdk/model/
uv run ruff format --check rock/sdk/model/server/ tests/unit/sdk/model/
```

如果 ruff format 报 diff,直接 `uv run ruff format rock/sdk/model/server/ tests/unit/sdk/model/` 修。代码写的时候我没跑 ruff,可能有 line-length / import 排序之类的小问题。

### 4.3 单测(已全部通过)

```bash
uv run pytest tests/unit/sdk/model/ -v
# → 47 passed in ~4s
```

**已验证通过的测试集**:
- `test_proxy.py` (27 个):routing/error/replay/header/cli/config/metrics
- `test_traj_recorder.py` (7 个):JSONL append/truncate/metrics/failure/missing payload/mkdir/rt fallback
- `test_traj_replayer.py` (11 个):cursor 加载/顺序/越界/reset/model mismatch/acompletion/streaming/exhaustion
- `test_model_client.py` (2 个):原有测试保留通过

**已知但不影响测试的边界情况**(生产注意):
- tool_calls 场景下 `_extract_assistant_text` 返回 `""`,replay 流式会返回空流(已知限制,不在本期范围)
- `litellm.callbacks` 是全局 list,测试隔离靠 patch,生产只起一次 server 无问题

### 4.4 集成验证(测试通过后)

#### Record 模式

```bash
# 终端 1
export OPENAI_API_KEY="sk-..."
export ROCK_MODEL_SERVICE_DATA_DIR=/tmp/rock-traj
mkdir -p /tmp/rock-traj
uv run python -m rock.sdk.model.server.main \
    --type proxy \
    --config-file examples/model_service/config_record.yaml \
    --port 8080

# 终端 2
curl -X POST http://127.0.0.1:8080/v1/chat/completions \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-3.5-turbo","messages":[{"role":"user","content":"say hi"}]}'

# 验证 traj
cat /tmp/rock-traj/LLMTraj.jsonl | jq '.id, .model, .response.choices[0].message.content'
# 应该看到 chatcmpl-xxx / gpt-3.5-turbo / "..."
```

#### Replay 模式

```bash
# 终端 1
uv run python -m rock.sdk.model.server.main \
    --type proxy \
    --replay-traj /tmp/rock-traj/LLMTraj.jsonl \
    --port 8081

# 终端 2 - 同样的 curl 打 8081
curl -X POST http://127.0.0.1:8081/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-3.5-turbo","messages":[{"role":"user","content":"anything (replay ignores msgs)"}]}'

# 应该返回与录制时同样的 response.choices[0].message.content
# 第二次 curl 会 404(traj exhausted),证明 cursor 在工作
```

#### Streaming 验证

```bash
curl -N -X POST http://127.0.0.1:8080/v1/chat/completions \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-3.5-turbo","stream":true,"messages":[{"role":"user","content":"count to 5"}]}'
# 应该看到 SSE chunks: data: {...}\n\n ... data: [DONE]\n\n
# traj 文件里那一行的 .stream == true,.response 是聚合后的完整 dict
```

#### Agent 端到端(最终验证)

`mini-swe-agent` 跑一个 SWE-bench 实例,base_url 指向 8080(record),完了用同 instance 接 8081(replay),期望 agent 最终生成的 patch 与录制时一致。这是最强 check,但跑起来麻烦,可以在 PR review 阶段再做。

---

## 5. Breaking Changes(PR 描述里必须写清楚)

### 5.1 traj 文件 schema 改变

`LLMTraj.jsonl` 每行从 `{"request": {...}, "response": {...}}` 变成 `StandardLoggingPayload`(几十个字段:`id/trace_id/model/messages/response/model_parameters/usage/startTime/endTime/status/...`)。

如果有下游消费者依赖旧的两字段 schema(脚本、UI、统计),会破坏。本期不提供"双格式输出"或"旧→新转换"工具,如有需要可单独写 `scripts/convert_traj.py`。

### 5.2 `traj_append` 默认值翻转

旧的 `ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE` 默认 `"false"` → `_write_traj` 用 `mode="w"`,实际表现是"每次调用覆盖,文件只剩最后一条"。新的 `ModelServiceConfig.traj_append` 默认 `True`(append-only)。

如果有人**之前依赖每次都覆盖来获取"最近一次调用"**(很罕见但可能),需要在 yaml 显式设 `traj_append: false`。

### 5.3 `error.type` 字段语义变化

旧值:固定字符串 `"proxy_retry_failed"`(retry 用尽)或 `"internal_error"`(其他)。
新值:litellm 异常类名,如 `"BadRequestError" / "RateLimitError" / "Timeout" / "AuthenticationError" / "APIError"`。

`error.message` 仍以 `"LLM backend error: ..."` 开头,关键字检测兼容。

### 5.4 `retryable_status_codes` 字段不再生效

旧版本根据 `retryable_status_codes` 白名单决定哪些状态码触发 retry(如 401 不 retry,429/500 retry)。新版本由 litellm 内部决定(对 `RateLimitError / APIError / Timeout / ServiceUnavailableError` 自动 retry,4xx 一般不 retry)。

字段保留在 yaml 不报错,但 handler 不读它。如果将来需要恢复白名单,见 3.4 节"fallback 方案"。

### 5.5 `stream=true` 不再被强制拒绝

旧版本对 `stream=true` 返回 400 + `"Streaming requests (stream=True) are not supported"`。新版本正常处理,返回 SSE。

如果有 client 之前**依赖** 400 来探测"是否启用流式",会破坏。但这种用法很反常,基本不会有。

### 5.6 `perform_llm_request` 函数已删除

下游不应该 import 这个 —— 它本来就是 proxy.py 内的 helper。如果有 test/script 直接 import 它,需要适配。`tests/unit/sdk/model/test_proxy.py` 我已改完。

### 5.7 新的依赖

`pip install rl-rock[model-service]` 会多装 litellm(及其依赖链:`openai>=1.x / tiktoken / aiohttp / tokenizers / ...`)。包大小 +~50MB。

---

## 6. 已知坑 / 接手时的注意事项

### 6.1 `local.py` 仍在 import `record_traj`

我**没改 local.py**(plan 明确"local 不动")。`local.py:12` 的 `from rock.sdk.model.server.utils import record_traj` 仍然成立,因为 utils.py 保留了 record_traj。如果接手者看到这个 import 想清理,**不要清理** —— 那会破坏 local 模式。

### 6.2 `litellm.callbacks` 是全局 list

`main.py:_configure_litellm_for_proxy` 用 `litellm.callbacks.append(recorder)`。如果同一进程多次启动(测试场景),会注册多次,导致每次调用落多份 traj。生产部署只跑一次没问题。**如果要写"重复初始化也安全"的逻辑**,可以改成 `if not any(isinstance(cb, TrajectoryRecorder) for cb in litellm.callbacks): litellm.callbacks.append(recorder)`。我没做,因为生产路径是"启动一次"。

同理 `litellm.custom_provider_map = [...]` 是赋值不是 append,所以 replay 重复初始化是幂等的。

### 6.3 SequentialCursor 在测试里要小心 cursor 跨用例

`SequentialCursor` 是实例属性 `self._idx`,每个测试自己 `SequentialCursor.load(p)` 都是新实例,不会跨用例污染。但如果有人写"模块级单例 replayer + 多个测试调它"的 fixture,会撞 idx。当前测试都是 per-test 实例,OK。

### 6.4 `litellm` import 较慢

litellm import 时会加载几个 OpenAI/HuggingFace 客户端,首次 import 可能 1-2 秒。`main.py` 把 `import litellm` 放在 `_configure_litellm_for_proxy()` 内部(函数级延迟 import),只在 proxy 模式启动时触发。`proxy.py` 是模块顶级 `import litellm`,handler 文件首次加载就触发 —— 这是 fastapi 路由注册时的开销,不影响请求路径性能。

### 6.5 `pyproject.toml` 的 `tzdata` 依赖

我看到 pyproject.toml 里 ide_diagnostics 报 `httpx/uuid/anyio/tzdata/...` 未安装 —— 这是 ide 当前 Python 环境没装 rock 主仓依赖,与本次改动无关。`uv sync` 后这些 hint 自动消失。

### 6.6 `__pycache__` 残留

旧 `proxy.py` 有 `__pycache__/proxy.cpython-310.pyc`。重写后第一次 import 会重新生成,**正常情况下没问题**。如果跑测试时报 `ImportError: cannot import name 'perform_llm_request'`,先 `find rock -name __pycache__ -exec rm -rf {} +` 清掉缓存。

### 6.7 别忘了 `extra_headers` 可能含敏感信息

`_filter_headers` 把所有非 hop-by-hop header 透传给上游,包括 client 传的 `Authorization`。这是**故意的** —— 让 client 自己带 API key 是 rock 现有约定。但意味着 traj 录的 `StandardLoggingPayload.metadata.headers`(如果有) 可能含 Bearer token。litellm 自己有 `turn_off_message_logging` / `redact_user_api_key_info` 等开关,**目前没启用**。如果将来 traj 文件要分发,需要先脱敏。

---

## 7. 不在本次范围 / 后续扩展(v2)

### 不在范围(明确不做)

- local 模式(`--type local`)的任何改动
- DB 持久化(traj 只走 JSONL)
- 旧 `{request, response}` traj 的兼容读取(replay 只接受新 schema)
- SWE-agent / OpenHands 原生 traj 格式互转
- replay 时 streaming 的细粒度时序还原(只保证 chunk 序列正确)
- tool_calls 的增量流式拆分(本期 streaming replay 只到 message-level chunk)

### 后续扩展(留了接口)

- **基于 messages hash 的乱序匹配**:`SequentialCursor` 旁加 `HashMatcher`,通过 `replay_mode: sequential | hash` 切换。当 agent 内部不严格按录制顺序调 LLM(分支/retry)时用。
- **多并发回放**:用请求 metadata 中的 `run_id` 路由到不同 cursor;`SequentialCursor` 改成 `dict[run_id, Cursor]`。
- **passthrough on miss**:cursor 用尽时回落到真 LLM(`import litellm; await litellm.acompletion(...)`)。用于"录到一半 traj 不够长"的调试场景。
- **`/admin/reset` HTTP 端点**:不重启 proxy 即可把 cursor 归零。
- **`scripts/convert_traj.py`**:把 SWE-agent `.traj` 或 OpenHands event log 转成 StandardLoggingPayload,反向也行。
- **traj 脱敏 hook**:写盘前过 `redact_keys: list[str]` 把指定字段抹掉。

---

## 8. 关键路径速查

### Rock 仓内(本次改动的)

| 路径 | 角色 |
|---|---|
| `pyproject.toml` | model-service extras 加 litellm |
| `rock/sdk/model/server/config.py` | ModelServiceConfig 新字段 |
| `rock/sdk/model/server/api/proxy.py` | 重写为 litellm SDK |
| `rock/sdk/model/server/main.py` | `_configure_litellm_for_proxy` + 新 CLI flags |
| `rock/sdk/model/server/utils.py` | 保留 record_traj 给 local |
| `rock/sdk/model/server/integrations/__init__.py` | 空,只为成包 |
| `rock/sdk/model/server/integrations/traj_recorder.py` | TrajectoryRecorder(CustomLogger) |
| `rock/sdk/model/server/integrations/traj_replayer.py` | SequentialCursor + TrajectoryReplayer(CustomLLM) |
| `rock/sdk/model/server/api/local.py` | **没改**(仍用 record_traj) |
| `tests/unit/sdk/model/test_proxy.py` | 改造完 |
| `tests/unit/sdk/model/test_traj_recorder.py` | 新 |
| `tests/unit/sdk/model/test_traj_replayer.py` | 新 |
| `examples/model_service/config_record.yaml` | 新 |
| `examples/model_service/config_replay.yaml` | 新 |

### litellm 仓(交叉验证用,在 `/mnt/xinshi/github/litellm/`)

| 关注点 | 路径 |
|---|---|
| CustomLogger 接口(基类) | `litellm/integrations/custom_logger.py:67` |
| CustomLLM 接口(基类) | `litellm/llms/custom_llm.py:47` |
| StandardLoggingPayload schema | `litellm/types/utils.py:2764` |
| streaming 聚合写入 payload | `litellm/litellm_core_utils/litellm_logging.py:1930-1955` |
| async_mock_completion_streaming_obj | `litellm/utils.py:6831` |
| custom_provider_map 加载流程(实际是怎么调 acompletion 的) | `litellm/main.py:4280-4326` |
| LiteLLM 异常基类(status_code 来源) | `litellm/exceptions.py` |

### 历史 / 对话产物

- 原始 plan 文件(详细设计推演): `/home/xinshi/.claude/plans/litellm-chat-completions-traj-replay-ser-lucky-rainbow.md`
- 已废弃的独立项目骨架: `/mnt/xinshi/github/litellm-traj/`(已 `rm -rf`)

---

## 9. 给接手者的 1 分钟上手

1. `cd /mnt/xinshi/github/Self-ROCK && uv sync --extra model-service --group test`
2. `uv run pytest tests/unit/sdk/model/ -v` → 应得 **47 passed**(已验证)
3. 跑集成验证(第 4.4 节)
4. 写 PR 描述,**重点说第 5 节的 breaking changes**
5. PR 评审里如果有人问"为什么不沿用 retry_async 的 status code 白名单",答:见第 3.4 节(litellm 默认 retry 已覆盖最常见场景,白名单后续可选加)

如果想了解整个项目背景而不只是这次 refactor,看顶层 `CLAUDE.md`。如果想知道 litellm 内部细节,看 `/mnt/xinshi/github/litellm/CLAUDE.md`(litellm 主仓的)。
