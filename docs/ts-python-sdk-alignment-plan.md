# TS SDK ↔ Python SDK 对齐方案

> 日期: 2026-06-18 | 状态: ✅ 已完成 | 测试: 843 全部通过

## 一、概述

ROCK 项目拥有 Python SDK（`rock/sdk/`）和 TypeScript SDK（`rock/ts-sdk/`）。本文档记录了使 TS SDK 功能与 Python SDK 对齐的完整方案和实施结果。

### 对齐原则

1. **以 Python SDK 为标准，TS SDK 单向追赶**
2. **TDD 流程**：每个文件先写测试（RED）→ 再写实现（GREEN）→ 重构（REFACTOR）
3. **保持平台惯用写法**：Python Pydantic → TS Zod；Python asyncio → TS Promise/async-await
4. **不可变模式**：使用 Zod schema + 工厂函数，避免 mutable 配置

## 二、差距分析

### 分析范围

```
Python SDK:  rock/sdk/ (84 files)
TS SDK:      rock/ts-sdk/src/ (41 files → 92 files after alignment)
```

### 差距总览

| 模块 | Python | TS(前) | 差距 |
|------|--------|--------|------|
| Sandbox Core | ✅ 完整 | ⚠️ 缺 delete/restart/commit/attach | 🟡 |
| OSS Client | ✅ 独立 OssClient | ⚠️ 内嵌 Sandbox | 🟡 |
| Speedup | ✅ 策略模式 | ⚠️ 内联实现 | 🟢 |
| Agent | ✅ RockAgent | ⚠️ DefaultAgent(基础) | 🟡 |
| EnvHub Datasets | ✅ 完整 | ❌ 无 | 🔴 |
| Bench Models | ✅ 完整 | ❌ 无 | 🔴 |
| Job/Trial System | ✅ 完整 | ❌ 无 | 🔴 |
| Model Server | ✅ 完整 | ❌ 无 | 🔴 |

## 三、实施方案

### Phase 1: Sandbox 层面补齐（4 个子任务）

| 任务 | 说明 | 新增文件 | 测试 |
|------|------|---------|------|
| Sandbox Extra API | delete/restart/commit/attach + parseErrorMessageFromStatus + namespace/experimentId | 修改 2 | 通过 |
| OSS Client 抽取 | 独立 OssClient class，两层 OSS 配置解析，async persistence | oss_client.ts | 17 |
| Speedup 策略重构 | executor + 3 策略类 + precheck | speedup/ (7 文件) | 通过 |
| Agent 增强 | RockAgent + Deploy.format + YAML 加载 + RuntimeEnv/ModelService 集成 | rock_agent.ts | 通过 |

### Phase 2: 数据和环境基础设施（2 个子任务）

| 任务 | 说明 | 新增文件 | 测试 |
|------|------|---------|------|
| EnvHub Datasets | DatasetClient + DatasetRegistry + OssDatasetRegistry + models | datasets/ (8 文件) | 55 |
| Bench 模型层 | 13 个 Zod schema 文件 (HarborJobConfig, RockEnvironmentConfig 等) | bench/ (16 文件) | 115 |

### Phase 3: Job/Trial 系统

| 子模块 | 说明 | 文件数 | 测试 |
|--------|------|--------|------|
| 基础层 (result, config, config_compose) | JobStatus, ExceptionInfo, TrialResult, JobConfig, ComposeJobConfig | 3 | 52 |
| Trial 抽象 | AbstractTrial, trial registry | 2 | 13 |
| Compose 基础设施 | resource_calculator, yaml_builder, script_builder | 3 | 37 |
| 具体 Trial | BashTrial, HarborTrial, ComposeTrial | 3 | 23 |
| 执行引擎 | Operator, JobExecutor, Job API | 3 | 18 |
| **合计** | **14 源文件 + 11 测试** | **25** | **143** |

### Phase 4: Model Server

| 子模块 | 说明 | 测试 |
|--------|------|------|
| server/config.ts | Zod schema + 常量 | 7 |
| server/sse.ts | SSE 编解码 | 20 |
| server/traj.ts | TrajectoryRecorder + SequentialCursor | 14 |
| server/file_handler.ts | 文件读写 | 7 |
| server/api/local.ts | 本地 API router | 4 |
| server/api/proxy.ts | ForwardBackend + ReplayBackend | 3 |
| server/main.ts | Express app factory | 2 |
| service.ts | ModelService 编排器 | 4 |
| **合计** | **10 文件** | **81** |

## 四、最终成果

### 测试统计

```
66 test suites, 843 tests, 0 failures
```

### 文件统计

| 指标 | 对齐前 | 对齐后 | 增长 |
|------|--------|--------|------|
| TS SDK 源文件 | 41 | 92 | +51 |
| TS SDK 测试文件 | 17 | 41 | +24 |
| 单元测试数量 | ~440 | 843 | +403 |

### Python ↔ TypeScript 模式映射

| Python | TypeScript |
|--------|-----------|
| `pydantic.BaseModel` | `z.object()` Zod schema |
| `@model_validator(mode="after")` | `.superRefine()` / `.transform()` |
| `ConfigDict(extra="forbid")` | `.strict()` |
| `@property` (computed) | `Object.defineProperties` in `.transform()` |
| `TYPE_CHECKING` import | `import type { ... }` |
| `asyncio.gather(*tasks)` | `Promise.all(tasks)` |
| `asyncio.Semaphore` | Promise pool (bounded concurrency) |
| `dataclass` | `interface` (plain data) |
| `yaml.safe_load/dump` | `js-yaml` parse/stringify |
| `subprocess.Popen` | `child_process.spawn` |
| FastAPI (server) | Express (server) |
| `httpx.AsyncClient` | `axios` |
| OTLP MetricsMonitor | winston logger |

## 五、新增依赖

- `express` + `@types/express` — Model Server HTTP 框架
- `ali-oss` — OSS 客户端（已有，OSS Client 使用）
- `js-yaml` — YAML 解析（已有）

## 六、执行团队

本对齐项目由 Agent Team (`ts-sdk-align`) 执行，采用以下工作流：
1. **规划** — 每个模块由 feature-dev:code-architect agent 先读 Python 源码，产出详细实现方案
2. **Review** — Team lead 审核方案，确认对齐准确性
3. **实现** — Agent 按 TDD 流程（RED → GREEN → REFACTOR）实现
4. **验证** — 完整测试套件验证，确保零回归
