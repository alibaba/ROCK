# CLI 每 Job 独立 Job ID 设计

## 目标

删除 CLI 和单任务 Planner 中进程本地的 `run_id`。每个 `PlannedJob` 使用独立 UUID `job_id`，用于 Job 身份、名称防冲突、环境变量和 JSONL Job 事件，不再制造一个未持久化的伪 Group/Run 标识。

## Planner

`PlannedJob` 增加 `job_id: UUID`。`SingleTaskPlanner.plan()` 支持调用方传入已有 `job_id`，未传入时自动生成 UUID。多任务和全量任务的 `job_name` 使用 `dataset_task_<job_id前8位>`，避免重复运行覆盖 OSS 产物；单任务模式继续保留用户提供的原始 `job_name`，满足显式续跑对原始路径和 session 的要求。

Planner 注入 `rock_job_id` label 和 `ROCK_JOB_ID` 环境变量，同时删除 `rock_run_id` 和 `ROCK_RUN_ID`。`task_id`、`ROCK_TASK_ID` 和 `ROCK_JOB_NAME` 保持不变。

## CLI

删除 `generate_run_id()`、`RunResult.run_id`、`UnifiedJobRunHandler.run_id` 以及命令层的生成和日志输出。`run_started` 与 `summary` 仍表示一次 CLI 调用的生命周期，但不携带 ID；`job_started`、`job_recovered`、`job_done` 事件携带对应 Job 的完整 UUID `job_id`。`progress` 只报告计数，不需要身份字段。

CLI 仍不访问数据库。这里生成的 `job_id` 是独立 Job 执行身份；元数据服务使用 Planner 时可以传入数据库已经生成的 `job_id`，保持执行结果与数据库记录一致。

CLI 提供可选参数 `--job-id UUID`，仅允许与一个明确的 `--task` 一起使用。传入时，CLI 将该 UUID 原样交给 Planner；未传入时仍由 Planner 自动生成。JSONL 的 `job_started`、`job_recovered` 和 `job_done` 事件始终返回最终实际使用的 `job_id`。`--tasks` 和 `--all` 模式不能接收单个 `--job-id`，其中的每个 Job 分别自动生成 UUID。

```bash
rock job run --job-config job.yaml --task task-1 \
  --job-id 12345678-1234-5678-1234-567812345678 \
  --jsonl
```

## 兼容性

本次会移除未文档化的 `ROCK_RUN_ID`、`rock_run_id` 和 JSONL `run_id` 字段，属于明确的 CLI 输出变更。单任务续跑参数、原始 `job_name`、sandbox/session/pid 逻辑及 OSS Viewer 不变。

## 验证

- 同一任务规划两次生成不同 UUID 和不同 Job 名称。
- 显式传入 UUID 时，Planner、名称、label 和环境变量使用同一个 ID。
- CLI `--job-id` 仅用于单任务，并原样传递到 Planner 和 JSONL Job 事件。
- CLI 未传 `--job-id` 时，每个 Job 仍自动生成独立 UUID。
- CLI Job 事件携带 `job_id`，批次事件不携带 `run_id`。
- 全局扫描 CLI 和 Planner 不再存在本地 `run_id`。
- SDK Planner、Executor 与 CLI Job 测试全部通过。
