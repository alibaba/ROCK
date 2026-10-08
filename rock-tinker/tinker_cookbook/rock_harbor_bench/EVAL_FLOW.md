# SWE-bench Eval 完整流程

## 概览

```
eval_swe_bench.py
  └── run_eval(config, tasks)
        ├── 初始化 TinkerClient / SamplingClient / Renderer / Policy
        └── asyncio.gather → 并发跑每个 evaluate_task
              └── RemoteSandboxEnv + do_single_rollout
                    ├── init_task_env()     → 服务端起沙箱
                    ├── get_step(0)         → 拿初始 prompt
                    ├── loop: sample → get_step(N)
                    └── episode 结束 → 收集 Trajectory
```

---

## 一、启动入口

**文件**：`tinker_cookbook/rock_harbor_bench/eval_swe_bench.py`

```bash
TINKER_API_KEY=tml-xxx python3 eval_swe_bench.py /path/to/SWE-Env.jsonl
```

`main()` 做两件事：

1. `load_harbor_tasks(data_path)` — 从 JSONL 文件读取任务列表，每行结构为：
   ```json
   {"extra_info": {"instance_id": "...", "dataset_name": "...", "dataset_type": "..."}}
   ```
   KEY_MAP 会把无下划线字段名（`instanceid`）自动转成有下划线版本（`instance_id`）。

2. 构造 `EvalConfig`，调用 `run_eval(config, tasks)`。

---

## 二、`run_eval`：初始化阶段

**文件**：`tinker_cookbook/eval.py`

### 2.1 创建客户端

```python
tinker_client = tinker.TinkerClient(base_url=config.base_url)
sampling_client = tinker_client.create_sampling_client(base_model=config.model_name)
```

- `TinkerClient` 本身不发请求，只做配置。`base_url` 通过 `**kwargs` 透传到底层 `AsyncTinker`。
- `create_sampling_client` 向服务端注册一个采样 Session，服务端加载对应模型，返回 `sampling_session_id`，后续所有采样请求都绑定在这个 Session 上。

### 2.2 创建 Renderer 和 Policy

```python
tokenizer = tokenizer_utils.get_tokenizer(config.tokenizer_path or config.model_name)
renderer  = get_renderer(renderer_name, tokenizer)   # 例如 Qwen3Renderer
policy    = TinkerTokenCompleter(sampling_client, max_tokens=8192, temperature=0.1)
```

- **Renderer**：负责消息 ↔ token 序列的双向转换。`build_generation_prompt(messages)` 把对话历史转成模型输入的 `ModelInput`；`parse_response(tokens)` 把生成的 tokens 解析回消息文字。
- **Policy**（`TinkerTokenCompleter`）：对 `SamplingClient.sample_async` 的封装，接收 `ModelInput`，返回生成的 `TokensWithLogprobs`。

### 2.3 并发跑任务

```python
await asyncio.gather(*[evaluate_task(task, ...) for task in tasks])
```

所有任务在同一个 event loop 里并发执行，共享同一个 `sampling_client` 实例（线程安全，内部有连接池）。

---

## 三、单个任务：`evaluate_task`

每个任务构造一个独立的 `RemoteSandboxEnv`，跑一个 rollout：

```python
env = RemoteSandboxEnv(
    task_info={"task_name": ..., "instance_id": ..., "dataset_name": ..., "dataset_type": ...},
    sampling_client=sampling_client,
    renderer=renderer,
    max_turns=config.max_turns,   # 默认 200
)
trajectory = await do_single_rollout(policy, env)
```

结果写入 `results_dir/`：
- `asummary.txt`：每个任务一行，含 task_name / reward / turns / 耗时 / PASS|FAIL|ERROR
- `aerr.txt`：异常任务的错误信息
- `{task_name}.txt`：轨迹详情（若配置了 tokenizer）

---

## 四、`do_single_rollout`：Episode 主循环

**文件**：`tinker_cookbook/rl/rollouts.py`

```python
transitions = []

# 循环外：初始化沙箱并拿第一个观测（只执行一次）
ob, stop_condition = await env.get_observation(init=True)
env_id = env.get_env_id()   # 沙箱起来后才有值，必须在 get_observation 之后调用

while True:
    ac_with_logprobs = await policy(ob, stop_condition, env_id=env_id)

    step_result = await env.step(
        ac_with_logprobs.tokens,
        extra=ActionExtra(stop_reason=ac_with_logprobs.stop_reason),  # stop reason 透传给服务端
    )

    transition = Transition(
        ob=ob,
        ac=ac_with_logprobs,
        reward=step_result.reward,
        episode_done=step_result.episode_done,
        metrics=step_result.metrics,
        logs=step_result.logs,
    )
    transitions.append(transition)

    ob = step_result.next_observation
    if step_result.episode_done:
        break
    stop_condition = step_result.next_stop_condition  # 在 break 之后更新，episode 结束时不更新

return Trajectory(transitions=transitions, final_ob=ob)
```

> **注意**：`get_observation(init=True)` 和 `get_env_id()` 在循环外只调用一次；`stop_condition` 在 `break` 判断之后才更新，确保 episode 结束时不被覆盖。

---

## 五（补）、自定义中间过程

如需在每轮 sample / step 前后插入自己的逻辑，直接把 `do_single_rollout` 的循环体展开，在三个位置插钩子：

```python
ob, stop_condition = await env.get_observation(init=True)
env_id = env.get_env_id()
transitions = []

while True:
    # ① 采样前：可以检查/修改当前 prompt
    my_preprocess(ob)

    ac_with_logprobs = await policy(ob, stop_condition, env_id=env_id)

    # ② 采样后、执行前：可以检查/记录模型生成的 action
    print(renderer.tokenizer.decode(ac_with_logprobs.tokens))

    step_result = await env.step(
        ac_with_logprobs.tokens,
        extra=ActionExtra(stop_reason=ac_with_logprobs.stop_reason),
    )

    # ③ 执行后：可以检查命令输出、决定是否提前终止等
    my_log(step_result)

    transition = Transition(
        ob=ob, ac=ac_with_logprobs, reward=step_result.reward,
        episode_done=step_result.episode_done,
        metrics=step_result.metrics, logs=step_result.logs,
    )
    transitions.append(transition)
    ob = step_result.next_observation
    if step_result.episode_done:
        break
    stop_condition = step_result.next_stop_condition

return Trajectory(transitions=transitions, final_ob=ob)
```

`env` 和 `policy` 保持不变，只在循环内加逻辑。`RemoteSandboxEnv` 负责消息历史和状态管理，不需要自己维护。

---

## 五、第一轮：`init_task_env` + `get_step(0)`

**文件**：`tinker_cookbook/rock_harbor_bench/env.py`

`env.get_observation(init=True)` 内部按顺序做两件事：

### 5.1 初始化沙箱

```python
env_id = await asyncio.to_thread(
    sampling_client.init_task_env,
    instance_id=task_info["instance_id"],
    dataset_name=task_info.get("dataset_name", "rock-harbor-bench"),
    dataset_type=task_info.get("dataset_type", "grpo"),
)
```

- **HTTP 接口**：`POST /api/v1/init_task_env`
- **请求体**：`InitTaskEnvRequest(instance_id, dataset_name, dataset_type)`
- **服务端行为**：拉取对应任务的代码仓库，起一个隔离的执行沙箱
- **超时**：600 秒
- **返回**：`env_id`（字符串，后续所有 `get_step` 的上下文句柄）

`asyncio.to_thread` 的作用：`init_task_env` 是同步阻塞调用，包一层避免阻塞 event loop，保证其他并发任务不受影响。

### 5.2 拿初始 Prompt

```python
step_response = await asyncio.to_thread(
    sampling_client.get_step, env_id=env_id, step_id=0
)
```

- **HTTP 接口**：`POST /api/v1/get_step`
- **请求体**：`GetStepRequest(env_id=env_id, step_id=0)`
- **返回** `GetStepResponse`：

| 字段 | 值 |
|------|----|
| `step_id` | `0` |
| `prompt` | `ModelInput`（任务描述 + 初始代码环境，已 tokenize） |
| `finish_reason` | `None`（刚开始，未结束） |
| `reward` | `None` |

`step_id=0` 是约定的起始步，若返回的 `step_id != 0` 说明沙箱状态异常（已被使用过）。

---

## 六、每一轮：`sample` → `get_step(N)`

### 6.1 模型采样

```python
# policy.__call__ 内部：
sample_result = await sampling_client.sample_async(
    prompt=ob,
    num_samples=1,
    sampling_params=SamplingParams(
        stop=stop_condition,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
    ),
    env_id=env_id,
)
```

- **HTTP 接口**：`POST /api/v1/asample`（异步采样）
- `env_id` 透传给服务端，服务端据此知道这次采样绑定哪个沙箱
- 内部有指数退避重试，遇到 429/5xx 自动重试

返回 `TokensWithLogprobs`：

| 字段 | 说明 |
|------|------|
| `tokens` | 生成的 token ID 列表（Agent 的操作，如 bash 命令） |
| `logprobs` | 每个 token 的 log 概率（训练时计算 loss 用） |
| `stop_reason` | `"stop"` 遇到 stop 序列 / `"length"` 到 max_tokens |

### 6.2 执行 Action，拿下一步状态

`env.step(action_tokens)` 内部：

```python
self._turn_count += 1
action_text = renderer.tokenizer.decode(action)
self._messages.append({"role": "assistant", "content": action_text})

step_response = await asyncio.to_thread(
    sampling_client.get_step, env_id=self._env_id, step_id=self._turn_count
)
```

服务端收到 `get_step(step_id=N)` 后，在沙箱里执行 Agent 上一步生成的命令，把执行结果（stdout/stderr）拼入对话 context，生成下一步的 prompt。

返回 `GetStepResponse`：

| 字段 | 说明 |
|------|------|
| `step_id` | `N` |
| `prompt` | 含本轮命令执行结果的新 `ModelInput` |
| `finish_reason` | `None` 继续 / 非 `None` 结束 |
| `reward` | 每步都有（中间步通常为 `0.0`，最后一步才给实际分数） |

---

## 七、Episode 结束判断

两个独立的终止条件，任一触发即结束：

| 条件 | 来源 | 处理 |
|------|------|------|
| `step_response.finish_reason is not None` | 服务端判断 | `episode_done=True`，reward 取服务端返回值 |
| `turn_count >= max_turns` | 客户端兜底 | 强制 `episode_done=True`，`reward=0.0` |

`finish_reason` 的三种值：

| 值 | 含义 |
|----|------|
| `finish` | 任务正常完成，Agent 解决了问题 |
| `exceed_max_step` | 服务端自身步数上限触发 |
| `exit` | Agent 生成了主动退出的信号 |

若 `get_step` 调用抛异常，`env.step` 的 `except` 分支会检查 `turn_count >= max_turns`，到了就强制结束，否则用 renderer 本地 build prompt 继续跑。

---

## 八、数据结构流转

```
JSONL
 └─ TaskInfo(instance_id, dataset_name, dataset_type)
      └─ init_task_env() → env_id
           └─ get_step(0) → ob₀: ModelInput
                └─ sample(ob₀, env_id) → ac₀: TokensWithLogprobs
                     └─ get_step(1) → ob₁, reward₀, finish_reason=None
                          └─ Transition(ob₀, ac₀, reward₀)
                               └─ sample(ob₁, env_id) → ac₁
                                    └─ get_step(2) → ob₂, reward₁, finish_reason="finish"
                                         └─ Transition(ob₁, ac₁, reward₁)

Trajectory(
    transitions = [Transition(ob₀, ac₀, r₀), Transition(ob₁, ac₁, r₁)],
    final_ob    = ob₂
)

TaskResult(
    task_name    = "instance-id",
    reward       = r₀ + r₁,
    turns_used   = 2,
    time_seconds = ...,
)
```

---

## 九、接口调用链

三个核心接口从用户代码到 HTTP 的完整调用路径：

### `sample`

```
do_single_rollout()                           rl/rollouts.py
  └── policy(ob, stop_condition, env_id)
        └── TinkerTokenCompleter.__call__()   tinker_cookbook/completers.py
              └── SamplingClient.sample_async()   lib/public_interfaces/sampling_client.py
                    └── SamplingClient.sample()
                          └── _sample_async_with_retries()
                                └── _sample_async_impl()
                                      └── _send_asample_request()
                                            └── client.sampling.asample()   resources/sampling.py
                                                  └── POST /api/v1/asample
```

`asample` 是异步接口，返回 `UntypedAPIFuture`（服务端的 future handle），`_sample_async_impl` 再通过 `_APIFuture.result_async()` 轮询拿到最终的 `SampleResponse`。

### `init_task_env`

```
do_single_rollout()                           rl/rollouts.py
  └── env.get_observation(init=True)
        └── RemoteSandboxEnv._init_sandbox()  rock_harbor_bench/env.py
              └── SamplingClient.init_task_env()   lib/public_interfaces/sampling_client.py
                    └── _init_task_env_async()
                          └── client.sampling.init_task_env()   resources/sampling.py
                                └── POST /api/v1/init_task_env
```

### `get_step`

```
do_single_rollout()                           rl/rollouts.py
  ├── env.get_observation(init=True)          （第一轮，step_id=0）
  │     └── RemoteSandboxEnv._get_step()      rock_harbor_bench/env.py
  │           └── SamplingClient.get_step()   lib/public_interfaces/sampling_client.py
  │                 └── _get_step_async()
  │                       └── client.sampling.get_step()   resources/sampling.py
  │                             └── POST /api/v1/get_step
  └── env.step(action_tokens)                 （后续每轮，step_id=N）
        └── RemoteSandboxEnv._get_step()      rock_harbor_bench/env.py
              └── SamplingClient.get_step()   lib/public_interfaces/sampling_client.py
                    └── _get_step_async()
                          └── client.sampling.get_step()   resources/sampling.py
                                └── POST /api/v1/get_step
```

### 各层职责

| 层 | 文件 | 职责 |
|----|------|------|
| Env / Policy | `rock_harbor_bench/env.py`、`completers.py` | 业务逻辑，管理对话轮次和沙箱状态 |
| SamplingClient | `lib/public_interfaces/sampling_client.py` | 并发调度、重试、subprocess 隔离 |
| AsyncSamplingResource | `resources/sampling.py` | 纯 HTTP 封装，构造请求体，打日志 |
| AsyncTinker | `_client.py` | httpx 异步 HTTP client，连接池管理 |

---

## 十、关键配置参数

| 参数 | 位置 | 作用 |
|------|------|------|
| `base_url` | `EvalConfig` | 推理服务地址，`TinkerClient` 通过 `**kwargs` 透传 |
| `model_name` | `EvalConfig` | 基座模型，决定加载哪个权重 |
| `tokenizer_path` | `EvalConfig` | 本地 tokenizer 路径，留空则从 HuggingFace 下载 |
| `renderer_name` | `EvalConfig` | prompt 格式，如 `qwen3`，不填则按模型名自动推断 |
| `dataset_name` / `dataset_type` | `EvalConfig` | 服务端用来定位任务数据集 |
| `max_turns` | `EvalConfig` | 客户端单个 episode 最大轮数，超出强制结束 |
| `max_tokens` | `EvalConfig` | 每次采样最多生成多少 token |
| `TINKER_SUBPROCESS_SAMPLING` | 环境变量 | 设为 `1` 时，采样在独立子进程执行，避免 GIL 阻塞 |

---

## 十一、服务端实现（ROLL-tinker）

> 以下描述服务端如何处理客户端发来的三个核心 HTTP 请求。服务端代码位于 `/roll/pipeline/tinker/`。

### 11.1 整体架构

```
FastAPI (api.py)
  ├── POST /api/v1/init_task_env  → TinkerEngine.init_task_env()
  ├── POST /api/v1/get_step       → TinkerEngine.get_step()
  └── POST /api/v1/asample        → 写入 FutureDB → 返回 future_id
        └── POST /api/v1/retrieve_future  （客户端轮询）→ 拿采样结果

TinkerEngine (engine.py)          ← 后台线程，持续轮询 SQLite
  └── RollBasePipeline (base_pipeline.py)  ← Ray 分布式后端
        └── _PipelineStateActor (Ray remote actor)  ← 所有共享状态
```

### 11.2 `init_task_env` 服务端处理

**文件**：`api.py:789` → `engine.py:349` → `base_pipeline.py:742`

```
POST /api/v1/init_task_env
  └── api.py: engine.init_task_env(instance_id, dataset_name, dataset_type)   [asyncio.to_thread]
        └── engine.py: self.backend.init_task_env(instance_id, ...)
              └── base_pipeline.py: ray.get(_state_actor.assign_task.remote(instance_id, ...))
                    └── _PipelineStateActor.assign_task()
                          ├── 找到空闲 env worker，分派任务
                          ├── env worker 开始执行（拉代码仓库、起沙箱）
                          └── 阻塞直到 env worker 第一次需要 model 推理
                                → 返回 env_id
```

`assign_task` 内部阻塞的原因：env worker 跑到需要 LLM 推理时，会调用 `submit_intercept_and_wait()`，此时 `assign_task` 才能返回，表明沙箱已就绪，等待第一次采样。

### 11.3 `asample` 服务端处理（两阶段异步）

**文件**：`api.py:1160` → `engine.py:722` → `base_pipeline.py:654`

#### 阶段一：提交请求

```
POST /api/v1/asample  (SampleRequest)
  └── api.py: FutureDB.create_committed_future(request)
        └── 写入 SQLite，生成 future_id
        └── 立即返回 UntypedAPIFuture(future_id)  ← 客户端拿到句柄后即可去干别的
```

#### 阶段二：引擎处理

```
TinkerEngine 后台循环:
  └── engine.py: process_pending_requests()
        └── find_batchable_sample()
              ├── 对 Roll 后端：backend.get_sample_readiness(env_id)
              │     └── _PipelineStateActor: 检查该 env 是否正在等待推理决策
              └── 选出可批处理的 sample → process_sample(prepared)
                    └── backend.sample(prepared)
                          └── base_pipeline.py: sample()
                                ├── _state_actor.provide_sample_decision(env_id, prompt_ids, sampling_params)
                                │     └── 解除 env worker 的 submit_intercept_and_wait() 阻塞
                                │           → vLLM 开始推理
                                └── _state_actor.get_turn_response(env_id)
                                      └── 阻塞等待 vLLM 输出
                                            → 返回生成的 token IDs
                                              → 写回 FutureDB（future 完成）
```

#### 阶段三：客户端轮询结果

```
POST /api/v1/retrieve_future  (future_id)
  └── api.py: 轮询 FutureDB，每 10s 检查一次，超时 50s
        ├── 若 future 已完成 → 返回 SampleResponse（含 token IDs + logprobs）
        └── 若超时 → 返回 408，客户端重新轮询
```

### 11.4 `get_step` 服务端处理

**文件**：`api.py:817` → `engine.py:368` → `base_pipeline.py:764`

```
POST /api/v1/get_step  (env_id, step_id)
  └── api.py: engine.get_step(env_id, step_id)
        └── base_pipeline.py: ray.get(_state_actor.get_step.remote(env_id, step_id))
              └── _PipelineStateActor.get_step()
                    ├── 若 step_id 对应的步已有历史记录 → 直接返回消息列表
                    ├── 若步骤尚未就绪（env 还在执行上一步命令）→ 等待 step_event
                    │     └── env worker 执行完命令后，调用 mark_turn_done() 触发 step_event
                    └── 返回 messages（含命令执行结果）
                          └── base_pipeline.py: messages_to_prompt_ids(messages, tokenizer)
                                → 转换为 token IDs → GetStepResponse
```

`api.py` 的 `get_step` 若遇到步骤未就绪（future 超时），会返回 **HTTP 408**；客户端（`SamplingClient.get_step`）识别 408 后重试，直到步骤就绪。

### 11.5 vLLM 推理拦截机制

这是 Roll 后端最关键的设计：让 vLLM 的推理完全受 Tinker SDK 控制。

```
env worker (Ray actor, 运行 SWE-bench agent):
  while episode_not_done:
      action = llm.generate(prompt)   ← vLLM 调用
             ↑
             │  TinkerProxyEnvManager 拦截推理请求
             │
             └── _PipelineStateActor.submit_intercept_and_wait(env_id, prompt_ids)
                   ├── 记录该 env 正在等待推理 → get_sample_readiness() 返回 True
                   └── 阻塞，直到 SDK 调用 sample(env_id)
                         └── provide_sample_decision(env_id, sdk_prompt_ids, sampling_params)
                               → 解除阻塞，vLLM 继续推理
                               → 推理完成后 record_turn_response(env_id, output_tokens)
                               → get_turn_response(env_id) 返回给 SDK
```

**关键不变式**：每一轮推理，env worker 和 SDK 之间有严格的"握手"：
1. env worker 先 `submit_intercept_and_wait` → 阻塞等待 SDK 决策
2. SDK `sample()` → `provide_sample_decision` → 解除阻塞 → vLLM 推理 → `record_turn_response`
3. 仍在 `sample()` 内：`get_turn_response(env_id)` 阻塞等待 vLLM 输出 → `sample()` 返回
4. SDK 调用 `get_step(env_id, N)` → 等待 env worker 在沙箱里执行完命令 → 拿到下一步观测

### 11.6 完整端到端时序

```
客户端 (eval_swe_bench.py)              服务端 (ROLL-tinker)

init_task_env(instance_id) ─────────→  assign_task()
                                          env worker 启动，跑到第一次 llm.generate()
                            ←─────────  env_id（沙箱就绪，等待第一次采样）

get_step(env_id, 0) ────────────────→  get_step() → 返回初始 prompt（任务描述）
                            ←─────────  GetStepResponse(prompt=ob₀)

sample(ob₀, env_id) ────────────────→  create_committed_future() → future_id
                            ←─────────  UntypedAPIFuture(future_id)

retrieve_future(future_id) ─────────→  引擎找到待处理 sample
                                        provide_sample_decision(env_id, ob₀)
                                          → vLLM 推理，生成 action tokens
                                        record_turn_response(env_id, tokens)
                            ←─────────  SampleResponse(tokens=ac₀)

（env worker 在沙箱里执行 ac₀ 对应的 bash 命令）

get_step(env_id, 1) ────────────────→  等待 env worker 执行完毕（step_event）
                                        命令输出拼入消息列表
                            ←─────────  GetStepResponse(prompt=ob₁, reward=r₀)

... 循环 ...

get_step(env_id, N) ────────────────→  env worker 判断任务完成
                            ←─────────  GetStepResponse(finish_reason="finish", reward=1.0)
```
