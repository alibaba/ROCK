# SWE-bench Train 流程

> 注意：本文档基于代码实现，尚未完成端到端联调，部分细节可能在联调后更新。

## 概览

```
train_swe_bench.py          （调试版，只跑一条任务）
train.py                    （完整版，多步训练循环）
  └── run_train(config, tasks)
        ├── 初始化 TrainingClient / SamplingClient / Renderer
        └── for step in train_steps:
              ├── asyncio.gather → 并发跑每个 rollout_task
              │     └── RemoteSandboxEnv + do_single_rollout   （与 eval 完全相同）
              ├── build_training_examples → Datum list + advantages
              ├── forward_backward_custom_async（PPO loss）
              ├── optim_step_async
              └── save_weights_and_get_sampling_client_async → 新 sampling_client
```

与 eval 的区别只在 rollout **之后**：eval 写结果文件，train 做一次参数更新。

---

## 一、入口

**调试版**：`tinker_cookbook/rock_harbor_bench/train_swe_bench.py`

```bash
TINKER_API_KEY=tml-xxx python3 tinker_cookbook/rock_harbor_bench/train_swe_bench.py \
    /path/to/SWE-Env.jsonl \
    --base-url http://127.0.0.1:9000
```

只取数据集第一条任务，跑完一次 rollout 后做一次训练步，方便端到端调试。

**完整版**：`tinker_cookbook/train.py`（库模块，无 CLI）

```python
from tinker_cookbook.train import HarborTask, TrainConfig, run_train

tasks = [HarborTask(task_name="instance-id-001"), ...]
config = TrainConfig(model_name="Qwen/Qwen3-4B-Instruct-2507", train_steps=10, batch_size=4)
step_results = await run_train(config, tasks)
```

---

## 二、初始化

### 2.1 创建 TrainingClient

```python
tinker_client = tinker.TinkerClient(base_url=config.base_url)

# 全新训练：创建 LoRA adapter
training_client = tinker_client.create_lora_training_client(
    base_model=config.model_name,
    rank=config.lora_rank,
)

# 断点续训：恢复权重 + optimizer 状态
training_client = tinker_client.create_training_client_from_state_with_optimizer(
    config.resume_path
)
```

### 2.2 获取初始 SamplingClient

```python
sampling_client = training_client.save_weights_and_get_sampling_client()
```

训练侧和采样侧是两个独立 session，权重通过 `save_weights_*` 显式同步。
每完成一次 optim_step 后都需要重新同步，否则采样用的还是旧权重。

---

## 三、训练循环

每一步（`step`）做四件事：

### 3.1 并发 Rollout

```python
raw = await asyncio.gather(
    *[rollout_task(task, policy, renderer, sampling_client, config) for task in task_batch]
)
trajectories = [t for t in raw if t is not None]
```

`rollout_task` 内部和 eval 完全一样：

```
RemoteSandboxEnv
  └── _init_sandbox()    → init_task_env   → env_id
  └── do_single_rollout
        ├── get_observation(init=True)  → get_step(0)  → ob₀
        └── loop: policy(ob) → env.step(action) → get_step(N)
              → Transition(ob, ac, reward)
        → Trajectory(transitions, final_ob)
```

`rollout_task` 失败时返回 `None`（不抛异常），保证 batch 内其他任务不受影响。

### 3.2 构造训练数据

```python
examples, old_logprobs, advantages = build_training_examples(trajectories)
```

- **`returns`**：每条轨迹的总 reward = `sum(transition.reward)`
- **`centered_adv`**：`total_return_i - mean(returns)`（batch 内中心化）
- **`examples`**：每个非空 transition → 一个 `Datum(model_input=ob, target_tokens=ac.tokens)`
- **`old_logprobs`**：`transition.ac.logprobs`（rollout 时采样已记录）
- **`advantages`**：shape 与 ac.tokens 相同的常数 tensor

单条轨迹（`train_swe_bench.py`）无法中心化，直接用 `total_return` 作 advantage。

### 3.3 PPO forward_backward

```python
loss_fn = make_ppo_loss(old_logprobs, advantages, clip_epsilon)
fwdbwd_future = await training_client.forward_backward_custom_async(examples, loss_fn)
fwdbwd_result = await fwdbwd_future.result_async()
```

`forward_backward_custom_async` 内部两阶段：
1. **forward**：服务端计算 `new_logprobs`，返回给客户端
2. **客户端执行 `loss_fn`**：

```python
ratio = exp(new_logprobs - old_logprobs)
loss  = -mean( min(ratio * adv, clip(ratio, 1±ε) * adv) )
loss.backward()   # 得到 dL/d(new_logprobs)
```

3. **backward**：把梯度传回服务端，服务端完成反向传播

### 3.4 参数更新

```python
optim_future = await training_client.optim_step_async(
    tinker.types.AdamParams(learning_rate=..., weight_decay=...)
)
optim_result = await optim_future.result_async()
```

Adam 参数默认：`beta1=0.9, beta2=0.95, eps=1e-8, weight_decay=0.0`。

### 3.5 同步权重给采样侧

```python
sampling_client = await training_client.save_weights_and_get_sampling_client_async()
```

返回一个新的 `SamplingClient`，下一步 rollout 使用更新后的权重。

---

## 四、数据结构流转

```
TaskInfo(task_name, instance_id, dataset_name, dataset_type)
  └── init_task_env() → env_id
        └── get_step(0) → ob₀: ModelInput
              └── sample(ob₀) → ac₀: TokensWithLogprobs(tokens, logprobs)
                    └── get_step(1) → ob₁, reward₀
                          └── Transition(ob₀, ac₀, reward₀)
                                └── ... (多轮)
                                      └── Trajectory(transitions, final_ob)

Trajectory × batch_size
  └── build_training_examples()
        └── Datum(model_input=ob, target_tokens=ac.tokens)  × transitions
            old_logprobs: list[Tensor]                        × transitions
            advantages:   list[Tensor]                        × transitions

forward_backward_custom_async(examples, ppo_loss)
  └── ForwardBackwardOutput(metrics={ppo_loss, ratio_mean, clipfrac})

optim_step_async(AdamParams)
  └── OptimStepResponse(metrics=...)

save_weights_and_get_sampling_client_async()
  └── SamplingClient  （下一步使用）
```

---

## 五、关键配置参数

| 参数 | 位置 | 作用 |
|------|------|------|
| `model_name` | `TrainConfig` | 基座模型，决定加载哪个权重 |
| `lora_rank` | `TrainConfig` | LoRA adapter 秩，影响可训练参数量 |
| `train_steps` | `TrainConfig` | 总训练步数 |
| `batch_size` | `TrainConfig` | 每步并发 rollout 的任务数 |
| `max_turns` | `TrainConfig` | 单个 episode 最大轮数 |
| `temperature` | `TrainConfig` | 采样温度（训练时通常 > 0 以保证探索） |
| `learning_rate` | `TrainConfig` | Adam 学习率 |
| `clip_epsilon` | `TrainConfig` | PPO clip 系数（默认 0.2） |
| `save_every` | `TrainConfig` | 每 N 步保存一次 checkpoint（0 = 不保存）|
| `resume_path` | `TrainConfig` | 断点续训的 checkpoint 路径 |

---

## 六、与 eval 流程的对比

| | eval（`eval.py`） | train（`train.py`） |
|---|---|---|
| 客户端类型 | `SamplingClient` | `TrainingClient` + `SamplingClient` |
| 并发方式 | `asyncio.gather`（所有任务） | `asyncio.gather`（batch 内） |
| rollout 代码 | `RemoteSandboxEnv` + `do_single_rollout` | **完全相同** |
| rollout 之后 | 写结果文件 | `forward_backward` + `optim_step` + 同步权重 |
| 结果 | `list[TaskResult]` | `list[StepResult]`（每步指标） |
