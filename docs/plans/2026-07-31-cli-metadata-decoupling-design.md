# CLI 元数据解耦与单任务断点续跑设计

## 背景与目标

Job/Group 元数据已经迁移到 PostgreSQL，并由可信服务端的元数据服务持有数据库凭证。CLI 运行在用户本地，既不应直接连接数据库，也不应继续把 OSS 当作元数据存储。因此 CLI 需要删除对旧 `RunMetaRepository`、`JobMetaRepository` 和 `JobViewer` 元数据接口的依赖。

CLI 仍保留两类本地能力：

1. 正常运行单个或多个任务。运行期间只维护进程内的进度、结果和汇总，不持久化 Group/Job 元数据。
2. 对一个已知任务进行断点续跑。调用方从远程元数据服务或其他可信来源取得运行句柄，再通过 CLI 参数传入。

OSS 仍然保存 Job/Trial 产物，所以 `job-list`、`job-show`、`trial-list` 和 `trial-show` 继续作为产物查询命令存在。

## 方案选择

采用“显式运行句柄”方案：`--resume-sandbox-id` 开启续跑模式，`--resume-pid` 指定原进程，`--resume-session` 可选。未指定 session 时，根据原始 `job_name` 推导为 `rock-job-{job_name}`。这一方案参数少、行为明确，而且不要求 CLI 了解数据库或元数据服务协议。

未采用的方案：

- 继续支持 `--resume RUN_ID`：需要 CLI 查询 Group 和 Job 元数据，与本地 CLI 不访问远程元数据的目标冲突。
- 在 CLI 中增加元数据服务客户端：会把认证、服务发现和 API 兼容性重新引入本地 CLI，当前需求不需要。
- 仅使用 sandbox ID：同一 sandbox 可能存在多个 session 和进程，无法可靠定位原任务。

## 命令与约束

单任务续跑示例：

```bash
rock job run \
  --job-config job.yaml \
  --task task-001 \
  --job-name original-job-name \
  --resume-sandbox-id sandbox-xxx \
  --resume-pid 12345
```

如原任务使用了非默认 session，可增加：

```bash
--resume-session custom-session
```

约束如下：

- `--resume-sandbox-id` 是续跑模式的开关。
- 续跑必须同时提供 `--resume-pid`，并且必须显式提供且仅提供一个 `--task`。
- 原始 `job_name` 可以来自 YAML；如果使用 flags 模式或 YAML 中没有正确的原始名称，必须通过 `--job-name` 提供。
- 续跑不能与 `--tasks`、`--all`、`--limit` 或大于 1 的并发度组合。
- 只提供 `--resume-pid` 或 `--resume-session` 而没有 `--resume-sandbox-id` 时直接报参数错误。
- 找不到 sandbox、session 或 pid 时返回失败，不自动创建新 sandbox，避免把“续跑”悄悄变成“重跑”。

删除 `run-list`、`run-status`。`job-show` 只接受明确的 Job artifact 名称，不再接受 `run_id + task_id` 反查。

## 组件与数据流

`ExistingJobHandle` 是与持久化模型无关的执行句柄，只包含 `sandbox_id`、`session`、`pid`。`JobExecutor.wait_existing_job(planned_job, handle)` 根据句柄重建 `TrialClient`，等待原进程结束并收集结果。它不查询数据库，也不负责判断任务是否应当续跑。

`JobCommand` 负责参数校验、加载配置、确定原始 `job_name`、推导默认 session，并构造 `ExistingJobHandle`。`UnifiedJobRunHandler` 接收可选句柄：普通运行调用 `run_job`；单任务续跑调用 `wait_existing_job`。Handler 不再生成本地 `run_id`；每个 `PlannedJob` 使用独立 UUID `job_id`，用于多任务名称防冲突、Job 环境变量和 JSONL Job 生命周期事件，但 CLI 不会把它持久化到数据库。

产物查询使用 `JobViewer` 的 OSS artifact API。通过 `--job-config` 定位产物时，直接由配置中的 `environment.oss_mirror` 创建 `JobViewer`，不经过任何元数据 Repository。`job-show` 直接读取 `result.json`；没有产物时返回明确提示。

## 错误处理与测试

参数组合错误由 run 子解析器返回退出码 2；远程 sandbox/process 恢复失败由执行器转换为失败结果，CLI 最终返回退出码 1。JSONL 事件保留 `run_started`、`job_recovered`、`job_done`、`progress` 和 `summary`，便于调用方自行记录。

测试覆盖：

- 新参数解析、默认 session 推导和显式 session 覆盖。
- 续跑缺少 task、pid、job_name，或组合多任务/并发参数时失败。
- 续跑只调用 `wait_existing_job`，且不会回退调用 `run_job`。
- 普通单/多任务运行不构造或写入元数据 Repository。
- `run-list`、`run-status` 和旧 `--resume RUN_ID` 不再被解析。
- `job-show` 只按 job name 读取 artifact，配置定位器直接构造 `JobViewer`。
