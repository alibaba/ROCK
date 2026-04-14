# `rock/sdk/bench/job.py` → `rock/sdk/job/api.py` + `trial/harbor.py` 替换分析

> 目的：判断新 Job 架构（`job/api.py` Facade + `JobExecutor` + `HarborTrial`）能否替换老 `bench/job.py` 中的单体 `Job` 类。

## 0. TL;DR

| 维度 | 结论 |
|------|------|
| **核心执行流程** | ✅ 1:1 对应，可替换 |
| **Harbor YAML / 脚本模板** | ✅ 完全一致 |
| **Sandbox 生命周期 / session / OSS 转发** | ✅ 已迁移到 `JobExecutor` |
| **Harbor 多子 trial 结果聚合** | ❌ **严重回归**，`HarborTrial.collect` 只返回 `trial_results[0]` |
| **Agent-aware wait timeout** | ❌ **回归**，丢失 `timeout_multiplier` + `agent.max_timeout_sec` 推导 |
| **`job_name` 自动生成** | ❌ 缺失 |
| **`namespace` / `experiment_id` 从 sandbox 回填** | ❌ 缺失 |
| **`JobResult.raw_output` / `exit_code` 填充** | ❌ 缺失 |
| **脚本 / 输出文件路径** | ⚠️ 从 `/data/logs/user-defined/` 变为 `/tmp/`，影响调试持久化 |
| **默认 wait 超时兜底** | ⚠️ 从 `DEFAULT_WAIT_TIMEOUT=7200` 变为 `config.timeout=3600` |

**结论：架构方向正确，但未完成迁移。当前直接替换会丢失 Harbor 的多 trial 结果和长时间 agent 支持。需补齐 7 项后方可下线 `bench/job.py`。**

---

## 1. 架构对比

### 1.1 老实现（单体）

```
rock/sdk/bench/job.py  (334 lines)
  └── class Job
        ├── __init__(config: HarborJobConfig)
        ├── run() / submit() / wait() / cancel()
        ├── _prepare_and_start      ─┐
        ├── _render_run_script       │  硬编码 Harbor 逻辑
        ├── _create_session          │  + sandbox 生命周期
        ├── _build_session_env       │  + 脚本渲染
        ├── _collect_results         │  + 结果收集
        ├── _generate_default_job_name
        ├── _autofill_sandbox_info
        ├── _get_wait_timeout        │  ← agent-aware 超时
        └── _upload_content          ─┘
```

### 1.2 新实现（分层）

```
rock/sdk/job/
  ├── api.py                  ← Job Facade (74 lines, 通用)
  ├── executor.py             ← JobExecutor: sandbox 生命周期 + nohup
  ├── operator.py             ← Operator / ScatterOperator
  ├── config.py               ← JobConfig / BashJobConfig 基类
  └── trial/
        ├── abstract.py       ← setup / build / collect 三阶段
        ├── registry.py       ← Config → Trial 注册表
        ├── bash.py           ← BashTrial
        └── harbor.py         ← HarborTrial (116 lines, 仅 Harbor 特有)

职责切分:
  Job       — 极薄 Facade, 组装 config + operator
  Operator  — 决定分发多少份 Trial
  Executor  — 并行启动 sandbox + 并行等待
  Trial     — 任务逻辑 (上传文件、生成脚本、解析结果)
```

---

## 2. 核心流程逐段对照

### 2.1 `submit()` 流程

| 步骤 | 老 `bench/job.py` | 新 `api.py` + `executor.py` + `harbor.py` | 状态 |
|------|-------------------|-------------------------------------------|------|
| 生成 job_name | `_generate_default_job_name()` (L273) | **缺失** | ❌ |
| 启动 sandbox | `Sandbox(env).start()` (L92) | `JobExecutor._do_submit` | ✅ |
| 回填 namespace/exp_id | `_autofill_sandbox_info()` (L303) | **缺失**（只靠 `HarborJobConfig._sync_experiment_id` validator 在 config 创建时强制） | ❌ |
| 创建 bash session | `_create_session()` (L218) | `JobExecutor._do_submit` → `create_session` | ✅ |
| OSS env 合并 | `_build_session_env()` (L207) | `JobExecutor._build_session_env` (L138) | ✅ 一致 |
| 上传 `file_uploads` | `sandbox.fs.upload_dir` 循环 (L163) | `AbstractTrial._upload_files` (L38) | ✅ |
| 上传 Harbor YAML | `_upload_content(to_harbor_yaml, ...)` (L170) | `HarborTrial.setup` → `write_file_by_path` | ✅ |
| 渲染 run script | `_render_run_script` (L188) | `HarborTrial.build` (L64) | ✅ **模板完全一致** |
| 上传 run script | `_upload_content(script, script_path)` (L171) | `JobExecutor._do_submit` → `write_file_by_path` | ⚠️ 路径不同（见 §3） |
| nohup 启动 | `start_nohup_process(bash script)` (L176) | `JobExecutor._do_submit` 同上 (L95) | ✅ |

### 2.2 `wait()` 流程

| 步骤 | 老实现 | 新实现 | 状态 |
|------|--------|--------|------|
| 计算 wait timeout | `_get_wait_timeout()`：`agent.max_timeout * multiplier + 600`，兜底 7200 | `config.timeout` 直接读（默认 3600） | ❌ **回归** |
| `wait_for_process_completion` | L104 | `JobExecutor._do_wait` L112 | ✅ |
| `handle_nohup_output` | L111 | 同上 L118 | ✅ |
| 收集 trial 结果 | `_collect_results()` 返回 **所有** trial JSON 打包成 `JobResult(trial_results=[all])` | `HarborTrial.collect()` 读所有 trial JSON **但只 `return trial_results[0]`** | ❌ **严重回归** |
| 写回 `raw_output` / `exit_code` | `result.raw_output = obs.output` (L121) | 从不设置，`JobResult.raw_output` 始终为 "" | ❌ |
| 失败状态传播 | `if not success: result.status = FAILED` | 通过 `exception_info` + `_build_result(all_success)` | ✅ 语义等价 |
| auto_stop | `self._config.environment.auto_stop` (L128) | `config.auto_stop` (L135) | ⚠️ **字段位置不同**：新代码读的是 `JobConfig.auto_stop` 基类字段，老代码读 `environment.auto_stop`。Harbor 的 `JobConfig` 继承后两者可能都存在，需确认用户填哪一个 |

### 2.3 `cancel()`

| 老 | 新 | 状态 |
|----|----|------|
| 单 pid `kill {pid}` | 遍历所有 `TrialClient` 逐个 kill | ✅ 新更通用 |

---

## 3. 关键回归与缺口（必须修复）

### G1 — Harbor 多子 trial 结果丢失（**BLOCKER**）

**现象**：
```python
# rock/sdk/job/trial/harbor.py:78
async def collect(self, sandbox, output, exit_code) -> BaseTrialResult:
    trial_results = await self._collect_trial_results(sandbox)
    if trial_results:
        return trial_results[0]        # ← 只取第一个，丢弃其他 N-1 个
```

**老行为**：`bench/job.py:233` 把所有子 trial 结果打包进 `JobResult.trial_results`：
```python
return JobResult(trial_results=trial_results)   # 完整列表
```

**架构错位**：老 Job 的 `JobResult.trial_results` 是 "一个 sandbox 执行 Harbor → 产出 N 个子 trial 结果"；新 `AbstractTrial.collect` 是 "一个 Trial 产出一个 `BaseTrialResult`"，`Job._build_result` 再把 M 个 trial 聚合。两套语义不对齐。

**修复选项**：
- **A** 改 `HarborTrial.collect` 返回一个 "wrapper" `TrialResult`，把所有 sub-trial 放进自定义字段（类型系统不干净）。
- **B** 改 `AbstractTrial.collect` 签名为 `→ list[TrialResult]`，让 `JobExecutor.wait` flatten（需要改动基础接口）。
- **C** 在 `HarborTrial` 中把"读 N 个 result.json"提前到 `ScatterOperator` 层：Operator 先 probe sandbox 预览 task 数，再 scatter N 份 Trial——但 Harbor 本身是一次性启动 orchestrator，没法这样拆。

**推荐 B**：改基础接口，`collect` 返回 `list[TrialResult]`，Bash 实现返回长度 1 的列表。这是语义最干净的修复。

### G2 — Agent-aware wait timeout 丢失

**老逻辑** (`bench/job.py:141-151`)：
```python
def _get_wait_timeout(self) -> int:
    multiplier = self._config.timeout_multiplier or 1.0
    agents = self._config.agents
    if agents:
        agent_timeout = agents[0].max_timeout_sec or agents[0].override_timeout_sec
        if agent_timeout:
            return int(agent_timeout * multiplier) + 600   # +600s 留给环境准备 / verifier
    return int(DEFAULT_WAIT_TIMEOUT * multiplier)          # 默认 7200s
```

**新逻辑** (`executor.py:115`)：`wait_timeout=config.timeout`，默认 `3600s`。

**影响**：任何配置了 `agent.max_timeout_sec > 3000` 的 Harbor job 都会被早杀；`timeout_multiplier`、`agent_timeout_multiplier` 等 5 个字段完全失效。

**修复**：在 `HarborJobConfig` 上加 `model_post_init`，把计算好的有效超时写回 `self.timeout`；或在 `HarborTrial.setup` 里动态调整；或 `JobExecutor` 允许 Trial override timeout（需接口扩展）。

### G3 — `job_name` 自动生成丢失

老：`{dataset_name}_{task_name if 单任务}_{uuid[:8]}`（`bench/job.py:273`）。

新：若用户不设 `job_name`，后续脚本路径、session 名全部使用 `"default"`，多实例并发会冲突。

修复：把 `_generate_default_job_name` 作为 `HarborJobConfig` 的 `model_validator`，或放进 `HarborTrial.setup`。

### G4 — `namespace` / `experiment_id` 从 sandbox 回填丢失

老 `_autofill_sandbox_info` 读取 `sandbox._namespace` / `sandbox._experiment_id` 校验并写回 config（L303）。新代码只在 config 构造时做 `_sync_experiment_id` 校验，sandbox 侧的真实值永远不会回传。

修复：在 `JobExecutor._do_submit` 拿到 sandbox 句柄后插入一次回填；或提供 `AbstractTrial.on_sandbox_ready(sandbox)` 钩子。

### G5 — `JobResult.raw_output` / `exit_code` 不被填充

`JobResult` 的字段存在，但新 Facade `_build_result` 从未写入（`api.py:67`）。

修复：`JobExecutor.wait` 把每个 Trial 的 `obs.output` / `obs.exit_code` 也返回；或在 `TrialResult` 上挂这两个字段，由 Job facade 聚合。

### G6 — 脚本 / 输出文件路径从持久化目录变成 `/tmp`

| | 脚本路径 | 输出路径 | Harbor YAML |
|---|----------|----------|-------------|
| 老 | `/data/logs/user-defined/rock_job_{name}.sh` | 同上 `.out` | `/data/logs/user-defined/rock_job_{name}.yaml` |
| 新 | `/tmp/rock_job_{name}.sh` | `/tmp/rock_job_{name}.out` | `/data/logs/user-defined/rock_job_{name}.yaml` |

`/tmp` 在容器重启后消失，线上调查失败 job 会更难。

修复：新 `JobExecutor._job_tmp_prefix` 改用 `USER_DEFINED_LOGS`，保持与 Harbor YAML 一致。

### G7 — `auto_stop` 字段迁移

老：读 `config.environment.auto_stop`（`RockEnvironmentConfig` 字段）。
新：读 `config.auto_stop`（`JobConfig` 基类字段）。

现状 `HarborJobConfig` 继承后两者同时存在，用户写在 `environment` 里的设置新架构会忽略。

修复：`HarborJobConfig` 加 `model_validator` 同步两个字段；或文档明确指引新字段。

---

## 4. 功能映射矩阵

| 功能 | 老位置 | 新位置 | 一致性 |
|------|--------|--------|--------|
| Facade `run/submit/wait/cancel` | `bench/job.py` `Job` 类 | `job/api.py` `Job` 类 | ✅ |
| Sandbox 启动 | `submit()` | `JobExecutor._do_submit` | ✅ |
| Session + OSS env | `_create_session` / `_build_session_env` | `JobExecutor._do_submit` / `_build_session_env` | ✅ |
| file_uploads 上传 | `_prepare_and_start` 循环 | `AbstractTrial._upload_files` | ✅ |
| Harbor YAML 上传 | `_upload_content` | `HarborTrial.setup` | ✅ |
| dockerd + setup + harbor run 脚本 | `_render_run_script` + `_RUN_SCRIPT_TEMPLATE` | `HarborTrial.build` + `_HARBOR_SCRIPT_TEMPLATE` | ✅ 模板字节级一致 |
| nohup 启动 | `start_nohup_process` | `JobExecutor._do_submit` | ✅ |
| nohup 等待 | `wait_for_process_completion` | `JobExecutor._do_wait` | ✅ |
| 子 trial 结果收集 | `_collect_results` | `HarborTrial._collect_trial_results` | ⚠️ 聚合方式不同（G1）|
| job_name 默认生成 | `_generate_default_job_name` | — | ❌ G3 |
| agent-aware 超时 | `_get_wait_timeout` | — | ❌ G2 |
| ns/exp_id 回填 | `_autofill_sandbox_info` | — | ❌ G4 |
| raw_output / exit_code | 手动写回 | — | ❌ G5 |
| script/out 路径 | `USER_DEFINED_LOGS` | `/tmp` | ⚠️ G6 |
| auto_stop | `environment.auto_stop` | `config.auto_stop` | ⚠️ G7 |

---

## 5. 替换路线图

```
Phase 1 — 补齐回归 (必做)
  1. 修 G1: 改 AbstractTrial.collect → list[TrialResult]，flatten 到 JobResult
  2. 修 G2: HarborJobConfig.model_post_init 计算有效 timeout，或 Trial 层 override
  3. 修 G3: _generate_default_job_name 挪到 HarborJobConfig validator
  4. 修 G4: JobExecutor 提供 on_sandbox_ready 钩子 + HarborTrial 回填
  5. 修 G5: TrialResult 加 raw_output/exit_code；_build_result 聚合
  6. 修 G6: _job_tmp_prefix 改用 USER_DEFINED_LOGS
  7. 修 G7: HarborJobConfig 同步 auto_stop 两处字段

Phase 2 — 测试等价
  1. tests/unit/sdk/agent/test_job.py 整套用 rock.sdk.job.Job 跑通
  2. tests/unit/sdk/agent/test_jobconfig_experiment_id.py 同上
  3. examples/harbor/harbor_demo.py 改用新 import 跑通

Phase 3 — 切换 + 标记 deprecated
  1. rock/sdk/bench/__init__.py 的 Job 指向 rock.sdk.job.Job
  2. rock/sdk/bench/job.py 整个文件标 DeprecationWarning，保留一个版本
  3. 文档引导迁移

Phase 4 — 移除
  1. 删 rock/sdk/bench/job.py
  2. bench/__init__.py 不再 re-export Job
```

---

## 6. 现有调用方影响面

```
tests/unit/sdk/agent/test_job.py            ← 大量 _build_session_env / _generate_default_job_name 私有 API 断言
tests/unit/sdk/agent/test_jobconfig_experiment_id.py
tests/unit/sdk/agent/test_models.py
tests/unit/sdk/job/test_integration.py
examples/harbor/harbor_demo.py              ← from rock.sdk.bench import HarborJobConfig, Job
rock/sdk/bench/__init__.py                  ← 顶层 re-export
```

Phase 1 完成后，`tests/unit/sdk/agent/test_job.py` 中基于私有方法 (`_build_session_env`, `_generate_default_job_name`) 的断言需要改写：
- `_build_session_env` → 测 `JobExecutor._build_session_env(config)` (已是 staticmethod)
- `_generate_default_job_name` → 测 `HarborJobConfig` 的 validator

---

## 7. 结论

新架构从**工程质量、可扩展性、职责划分**角度都优于老实现；但目前 `HarborTrial` 只是"把核心脚本搬过去"，`bench/job.py` 的 **Harbor 特有补齐逻辑**（多子 trial 聚合、agent-aware 超时、job_name 生成、ns/exp_id 回填）**尚未迁移**。

**当前阶段不建议直接删除 `bench/job.py`**。先按 §5 Phase 1 补齐 G1–G7 七项，跑通现有 Harbor 测试，再切换入口、标 deprecated、最终移除。
