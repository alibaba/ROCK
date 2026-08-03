# Job ID 元数据查询设计

## 目标

`job_id` 是 Job 的唯一标识。SDK 通过 `job_id` 查询单个 Job 的完整元数据，调用方从同一份数据中读取状态、执行句柄、结果、错误和时间信息，并自行决定是否续跑、重试或不处理。

## 设计

- `JobMetadataRepository.get_job(job_id)` 保持为通用单 Job 查询入口，不增加仅面向续跑的重复接口。
- 返回值继续使用 `JobMeta`，覆盖数据库 `job_metadata` 表的全部字段，包括 `job_name`、`task_id`、`status`、`sandbox_id`、`session`、`pid`、`exit_code`、`score`、`error` 和各时间字段。
- `job_name` 允许重复，只作为运行配置和产物路径的一部分，不能用于唯一查询或更新元数据。
- `Job` 执行门面接受可选的 `job_id`。元数据服务先创建记录时，将该 UUID 传给 `Job`；独立使用 SDK 时，由 `Job` 自动生成 UUID。
- `JobResult.job_id` 返回上述真实 UUID 的字符串形式，不再返回 `job_name`。
- CLI 不读取数据库，也不通过 `job_name` 定位元数据；显式单任务恢复参数的行为保持不变。

## 状态判断边界

SDK 返回原始 `status` 和完整字段，不把“需要继续处理”“挂接已有进程”“失败后重新提交”合并成一个布尔值。客户端可根据业务策略判断：`completed` 通常无需处理，`running` 且执行句柄完整时可以挂接，失败类状态可重新提交。这样不会把通用元数据查询绑定到某一种续跑策略。

## 验证

- 使用相同 `job_name` 创建两个 Job，通过不同 `job_id` 查询时得到各自完整数据。
- 更新状态和执行句柄后，`get_job(job_id)` 返回所有最新字段。
- 显式传入 `job_id` 时，`JobResult` 返回相同 ID。
- 未传入 `job_id` 时，不同 `Job` 实例生成不同 UUID。
