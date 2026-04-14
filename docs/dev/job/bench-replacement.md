# `rock/sdk/bench/job.py` → `rock/sdk/job/api.py` + `trial/harbor.py` 替换分析

> 目的：判断新 Job 架构（`job/api.py` Facade + `JobExecutor` + `HarborTrial`）能否替换老 `bench/job.py` 中的单体 `Job` 类。

## 0. TL;DR

| 维度 | 结论 |
|------|------|
| **核心执行流程** | ✅ 1:1 对应，可替换 |
| **Harbor YAML / 脚本模板** | ✅ 完全一致 |
| **Sandbox 生命周期 / session / OSS 转发** | ✅ 已迁移到 `JobExecutor` |
| **Harbor 多子 trial 结果聚合** | ✅ (G1) `AbstractTrial.collect` 返回 `TrialResult \| list[TrialResult]`；`Job._build_result` 按 `isinstance` 拍平 |
| **Agent-aware wait timeout** | ✅ (G2) `HarborJobConfig._compute_effective_timeout` 写回 `self.timeout` |
| **`job_name` 自动生成** | ✅ (G3) `HarborJobConfig._auto_job_name` validator |
| **`namespace` / `experiment_id` 从 sandbox 回填** | ✅ (G4) `AbstractTrial.on_sandbox_ready` 默认实现（所有 Trial 继承） |
| **`JobResult.raw_output` / `exit_code` 填充** | ✅ (G5) `TrialResult.raw_output/exit_code` + `JobExecutor._do_wait` 写回 + `Job._build_result` 聚合 |
| **脚本 / 输出文件路径** | ✅ (G6) `JobExecutor._job_tmp_prefix` 回到 `USER_DEFINED_LOGS` |
| **`auto_stop` 两处字段同步** | ✅ (G7) `HarborJobConfig._sync_auto_stop` OR 语义 validator |

**结论：G1-G7 已于 #783 全部修复，blue-green 等价测试锁死契约；`rock/sdk/bench/Job` 已加 `DeprecationWarning`，计划 1.7.x 下线。**

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
- **B** 改 `AbstractTrial.collect` 签名为 `→ TrialResult | list[TrialResult]`（union）：单结果 Trial（如 `BashTrial`）保持返回 `TrialResult`，多结果 Trial（如 `HarborTrial`）返回 `list[TrialResult]`；`Job._build_result` 在 `JobResult.trial_results` 聚合时根据实际返回类型 `isinstance(r, list)` 决定 `extend` 还是 `append`。
- **C** 在 `HarborTrial` 中把"读 N 个 result.json"提前到 `ScatterOperator` 层：Operator 先 probe sandbox 预览 task 数，再 scatter N 份 Trial——但 Harbor 本身是一次性启动 orchestrator，没法这样拆。

**推荐 B（union + 拍平）**：接口上明确允许单/多两种返回形态，`BashTrial` 继续返回单个 `TrialResult` 无需改造，`HarborTrial` 返回完整 `list[TrialResult]`；`Job` Facade 在 `_build_result` 统一 flatten 到 `JobResult.trial_results`。这样既最小扰动下游 Trial 实现，又保留干净的类型签名。

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
  1. 修 G1: 改 AbstractTrial.collect → TrialResult | list[TrialResult]（union），
           Job._build_result 按 isinstance 判断 extend/append 到 JobResult.trial_results
  2. 修 G2: HarborJobConfig.model_post_init 计算有效 timeout，或 Trial 层 override
  3. 修 G3: _generate_default_job_name 挪到 HarborJobConfig validator
  4. 修 G4: JobExecutor 调用 on_sandbox_ready 钩子 + AbstractTrial 提供回填默认实现
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

新架构从**工程质量、可扩展性、职责划分**角度都优于老实现；G1-G7 7 项回归/缺口均已在 #783 补齐，blue-green 等价测试锁死两路径契约。

**现阶段：** `rock/sdk/bench/Job` 已加 `DeprecationWarning`，仍保留作为兼容层；新代码应统一用 `rock.sdk.job.Job` + `HarborJobConfig`。计划 1.7.x 版本移除 blue 路径。

---

## 8. 修复状态（2026-04-14，PR #783）

| Gap | Fix | 相关 commit |
|-----|-----|------------|
| G1 — 多子 trial 结果聚合 | `AbstractTrial.collect` 返回 union；`Job._build_result` 按 `isinstance` 拍平 | `fix(job/G1a)` `fix(job/G1b)` |
| G2 — effective wait timeout | `HarborJobConfig._compute_effective_timeout` validator | `fix(job/G2)` |
| G3 — 自动 job_name | `HarborJobConfig._auto_job_name` validator | `fix(job/G3)` |
| G4 — sandbox 回填 ns/exp_id | `AbstractTrial.on_sandbox_ready` 默认实现回填 + 校验（HarborTrial / BashTrial 共享） | `fix(job/G4)` `refactor(job): hoist to AbstractTrial` |
| G5 — `JobResult.raw_output/exit_code` | `TrialResult` 加字段；`JobExecutor._do_wait` 写回；`Job._build_result` 聚合 | `fix(job/G5)` |
| G6 — 脚本/输出持久化 | `_job_tmp_prefix` 使用 `USER_DEFINED_LOGS` | `fix(job/G6)` |
| G7 — `auto_stop` OR 同步 | `HarborJobConfig._sync_auto_stop` validator | `fix(job/G7)` |
| blue-green 等价锁 | 4 tests 交叉验证同一 config 两路径产出一致 `JobResult` | `test(job): blue-green equivalence` |

测试：222 → 250 passing（+28，零回归）。
