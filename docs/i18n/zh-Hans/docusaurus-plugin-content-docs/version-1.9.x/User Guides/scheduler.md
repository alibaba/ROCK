---
sidebar_position: 5
---

# 任务调度器(Scheduler)

ROCK 调度器是内嵌于 `admin` 服务中的周期性任务框架。它会按可配置的时间间隔,把后台维护任务(镜像清理、文件清理、容器清理、镜像预拉取、自定义任务……)分发到所有存活的 Ray worker 上,从而在无人工干预的情况下保持 worker 节点健康。

本文介绍如何启用调度器、配置内置任务、编写自定义任务,以及如何观测运行状态。

## 1. 工作原理

- 调度器以独立的守护线程(`SchedulerThread`)运行在 `admin` 进程内,使用自己的 `asyncio` 事件循环。
- 任务通过 [APScheduler](https://apscheduler.readthedocs.io/) 以固定间隔(`interval_seconds`)触发。
- 每次触发时,调度器会先获取存活 Ray worker 列表(由 `worker_cache_ttl` 秒级缓存),然后并发地把任务下发到每个 worker(默认并发度:50)。
- **下发动作通过 worker 上的 rocklet 服务以 HTTP 方式完成**:admin 端构造 `RemoteSandboxRuntime(host=worker_ip, port=Port.PROXY)`(参见 `rock.deployments.constants.Port`),并调用 `runtime.execute / read_file / write_file`。**因此每个 worker 都必须运行 `rocklet` 服务并在 `Port.PROXY` 上可达**,否则调度器无法下发命令、也无法在 worker 上读写状态文件。
- 每个任务都继承自 `rock.admin.scheduler.task_base.BaseTask`,并必须实现 `run_action(runtime: RemoteSandboxRuntime)` —— 单个 worker 上的实际执行逻辑。
- 每个 worker 的执行状态会被持久化到 `ROCK_SCHEDULER_STATUS_DIR`(默认 `/data/scheduler_status`)目录下的 JSON 文件中;每次执行结束后还会写入聚合报告 `<status_dir>/<task_type>_run_report.json`。
- 当配置了 Nacos 配置源时,调度器会订阅配置变更,并按 diff 应用:仅 hash 发生变化的任务被重新安装,被删除的任务会同步从所有 worker 上清理。

### 前置条件

启用调度器之前,请确认每个 Ray worker 满足以下条件:

| 条件 | 原因 |
|------|------|
| worker 上正在运行 `rocklet` 进程 | 调度器通过 rocklet HTTP 接口下发每一个任务;若 rocklet 不存在,`runtime.execute` 调用会超时。 |
| admin 可访问 rocklet 监听端口 | 调度器固定使用 `Port.PROXY`(定义于 `rock.deployments.constants.Port`)作为下发目标,请确认防火墙 / 安全组未阻断该端口。 |
| `ROCK_SCHEDULER_STATUS_DIR` 在 worker 内可写 | 任务会在该目录下读写 `<task>_status.json`,用于幂等控制和 PID 跟踪。 |
| 任务依赖的工具在 worker 上可用 | 例如清理 / 拉取类任务需要 `docker`;`ImageCleanupTask` 首次运行会通过 `curl` 联网安装 `docuum`。 |

rocklet 服务由 worker 标准启动脚本(`docker_run.sh`、`docker_run_with_uv.sh`、`docker_run_with_pip.sh`)自动拉起,通常等价于 `rocklet --port <Port.PROXY>`。如果你使用了自定义 entrypoint 来启动 worker,请确保等价命令被执行。具体的运行时类型与 rocklet 启动方式可参考 [Configuration](./configuration.md)。

### 幂等性

每个任务都需要声明自己的幂等模式,该模式直接影响重复触发时的行为:

| 模式 | 行为 |
|------|------|
| `IDEMPOTENT` | 每次 tick 都会执行,可安全重复(例如 `docker pull`、`find -exec rm`)。 |
| `NON_IDEMPOTENT` | 任务会拉起一个后台守护进程(例如 `docuum`)。调度器会读取上一次的状态文件,检查记录的 PID 是否仍存活,若仍在运行则跳过本次启动。当任务从配置中移除时,调度器会通过 `pkill` 杀掉该进程。 |

## 2. 启用调度器

调度器配置位于 ROCK admin YAML 顶层 `scheduler:` 字段下(例如 `rock-conf/rock-local.yml`、`rock-conf/rock-dev.yml`)。

```yaml
scheduler:
  enabled: true              # 总开关
  worker_cache_ttl: 43200    # Worker IP 缓存 TTL(秒)
  tasks:
    # ... 任务列表,详见下文
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 总开关。设为 `false` 时所有任务会被卸载,且不再触发。 |
| `worker_cache_ttl` | int | `3600` | 存活 worker IP 列表缓存时长(秒);超过该时长后会从 `ray.nodes()` 重新获取。 |
| `tasks` | list | `[]` | `TaskConfig` 列表,详见 [第 4 节](#4-任务配置schema)。 |

### 相关环境变量

| 变量 | 默认值 | 用途 |
|------|--------|------|
| `ROCK_SCHEDULER_STATUS_DIR` | `/data/scheduler_status` | worker 上写入任务状态 JSON 与执行报告的目录。 |
| `ROCK_LOGGING_PATH` | (未设置) | 设置后,调度器拉起的守护进程(docuum、container_cleanup、image_pull)会将 stdout/stderr 重定向到 `<ROCK_LOGGING_PATH>/<task>.log`。 |
| `ROCK_DOCUUM_INSTALL_URL` | `https://raw.githubusercontent.com/stepchowfun/docuum/main/install.sh` | `ImageCleanupTask` 按需拉取 `docuum` 安装脚本的 URL。 |

## 3. 内置任务

ROCK 在 `rock.admin.scheduler.tasks` 下提供了 4 个内置任务,通过把 `task_class` 设置为对应的全限定类路径即可注册。

### 3.1 ImageCleanupTask

在每个 worker 上运行 [`docuum`](https://github.com/stepchowfun/docuum),当磁盘占用超过阈值时按 LRU 策略淘汰镜像。**非幂等** —— `docuum` 是常驻守护进程;调度器会跟踪其 PID,只要进程仍存活就跳过重复拉起。

```yaml
- task_class: rock.admin.scheduler.tasks.image_cleanup_task.ImageCleanupTask
  enabled: true
  interval_seconds: 43200       # 每 12 小时检查一次守护进程
  params:
    disk_threshold: "70%"       # 磁盘占用超过 70% 时触发淘汰
    image_whitelist:             # 匹配 repository:tag 的 glob 模式,白名单内的镜像不会被淘汰
      - "python:3.11"
      - "my-registry.example.com/base/*"
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `disk_threshold` | str | `"1T"` | 传给 `docuum --threshold` 的磁盘阈值,支持容量(`100G`、`1T`)或百分比(`70%`)。 |
| `image_whitelist` | list[str] | `[]` | 透传给 `docuum --keep` 的 glob 模式列表。 |

### 3.2 FileCleanupTask

遍历配置的目录,删除超过 `max_age_mins` 或大于 `max_file_size` 的文件,然后清理留下的空目录。**幂等**。

```yaml
- task_class: rock.admin.scheduler.tasks.file_cleanup_task.FileCleanupTask
  enabled: true
  interval_seconds: 86400       # 每天执行一次
  params:
    target_dirs:
      # 字符串形式 —— 不配置排除项
      - "/data/service_status"
      # 对象形式 —— 配置该目录独有的排除项
      - path: "/data/logs"
        exclude_files:           # 支持纯文件名 / 相对路径 / 绝对路径
          - "docuum.log"
          - "./rocklet.log"
          - "./access.log"
        exclude_dirs:
          - ".cache"
    max_age_mins: 10080          # 7 天,超出此时间的文件会被删除
    max_file_size: "1G"          # 大于此大小的文件会被删除(支持 K/M/G/T)
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `target_dirs` | list | `[]` | 每个条目是一个字符串(只填路径)或 `{path, exclude_files, exclude_dirs}`。 |
| `max_age_mins` | int | `10080` | mtime 早于该分钟数的文件会被删除。 |
| `max_file_size` | str | `"1G"` | 大于该阈值的文件会被删除,支持 `K/M/G/T` 单位。 |

删除条件为 `(-mmin +max_age_mins) OR (-size +max_file_size)`。文件清理后,会再用 `find -depth -type d -empty -delete` 清理留下的空目录(同样遵循 `exclude_dirs` 配置)。

### 3.3 ContainerCleanupTask

删除停止时间超过指定时长的 Docker 容器,避免 worker 上的容器列表无限增长。**幂等**。

```yaml
- task_class: rock.admin.scheduler.tasks.container_cleanup_task.ContainerCleanupTask
  enabled: true
  interval_seconds: 86400
  params:
    max_age_hours: 72            # 删除超过 72 小时的 exited 容器
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_age_hours` | int | `24` | 已退出容器的最大保留时间(以 `FinishedAt` 起算的小时数);超出后被 `docker rm`。 |

每次执行还会顺带清理处于 `created` 状态(从未启动)的容器。

### 3.4 ImagePullTask

在每个 worker 上预拉取一组 Docker 镜像,并可选地先登录私有仓库,以降低沙箱冷启动延迟。**幂等**(若镜像已是最新,`docker pull` 等同于空操作)。

```yaml
- task_class: rock.admin.scheduler.tasks.image_pull_task.ImagePullTask
  enabled: true
  interval_seconds: 21600       # 每 6 小时刷新一次
  params:
    images:
      # 字符串形式 —— 公开镜像,无需鉴权
      - "python:3.11"
      # 对象形式 —— 私有镜像,需要登录
      - image: "my-registry.example.com/chatos/python:313"
        registry_username: "myuser"
        registry_password: "bXlwYXNzd29yZA=="   # base64 编码
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `images` | list | `[]` | 每个条目是一个镜像字符串或 `{image, registry_username, registry_password}`。 |

`registry_password` 必须是 base64 编码,worker 端会先解码再通过 `docker login --password-stdin` 登录。仓库地址会从镜像名称中解析,因此每个镜像可以指向不同的仓库。

## 4. 任务配置 Schema

`scheduler.tasks` 下的每一项都会被解析为 `rock.config.TaskConfig`:

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `task_class` | str | `""` | Python 类的全限定路径,**必填**。 |
| `enabled` | bool | `true` | 设为 `false` 的任务在加载阶段会被跳过,在 reload 阶段会被卸载。 |
| `interval_seconds` | int | `3600` | APScheduler `interval` 间隔(秒)。 |
| `params` | dict | `{}` | 任务自定义参数,会在 `from_config()` 中被消费。 |

只要某个任务条目中任何字段发生变化,调度器就会卸载旧任务(非幂等任务还会清理 worker 上的守护进程与状态文件)再安装新任务,**整个过程不需要重启 admin 进程**。

## 5. 编写自定义任务

任何位于 Python 路径下、继承自 `BaseTask` 的类都可以注册为调度任务。最小契约示例如下:

```python
# my_pkg/my_tasks/disk_report_task.py
from rock.admin.proto.request import SandboxCommand as Command
from rock.admin.scheduler.task_base import BaseTask, IdempotencyType, TaskStatusEnum
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime


class DiskReportTask(BaseTask):
    """记录每个 worker 的 `df -h` 输出。"""

    def __init__(self, interval_seconds: int = 3600, mount_point: str = "/"):
        super().__init__(
            type="disk_report",                   # 同时作为 APScheduler job id 与状态文件名前缀
            interval_seconds=interval_seconds,
            idempotency=IdempotencyType.IDEMPOTENT,
        )
        self.mount_point = mount_point

    @classmethod
    def from_config(cls, task_config) -> "DiskReportTask":
        return cls(
            interval_seconds=task_config.interval_seconds,
            mount_point=task_config.params.get("mount_point", "/"),
        )

    async def run_action(self, runtime: RemoteSandboxRuntime) -> dict:
        result = await runtime.execute(
            Command(command=f"df -h {self.mount_point}", shell=True),
        )
        return {
            "status": TaskStatusEnum.SUCCESS,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
        }
```

随后在 YAML 中注册:

```yaml
scheduler:
  enabled: true
  tasks:
    - task_class: my_pkg.my_tasks.disk_report_task.DiskReportTask
      enabled: true
      interval_seconds: 600
      params:
        mount_point: "/data"
```

### 自定义任务编写要点

- **`super().__init__()` 中的 `type` 必须全局唯一**:它会同时被用作 APScheduler job id、状态文件名(`<type>_status.json`)与执行报告文件名(`<type>_run_report.json`)。两个任务不能共用同一个 `type`。
- **正确选择 `IdempotencyType`**:
  - 当 `run_action` 同步执行完毕、可安全重入时,使用 `IDEMPOTENT`。
  - 当任务通过 `nohup` 拉起常驻守护进程并返回 PID 时,使用 `NON_IDEMPOTENT`;调度器会跟踪 PID,在其存活期间跳过重复启动,在卸载时通过 `pkill` 杀掉。
- **`run_action` 必须返回 dict**,推荐字段:
  - `status` —— `TaskStatusEnum` 值,会写入状态文件。
  - `pid` —— `NON_IDEMPOTENT` 守护型任务必须返回(可使用 `rock.utils.system.extract_nohup_pid` 从 `nohup ... & echo PID_PREFIX${{!}}PID_SUFFIX` 输出中提取)。
  - 其他诊断字段会落到状态文件的 `extra` 块里。
- **重写 `from_config(cls, task_config)`** 用于把 `task_config.params` 翻译成 `__init__` 的入参。
- **使用 `runtime.execute / read_file / write_file`** 与 worker 通信,**不要**在本地直接执行 shell —— 调度器是把任务下发到远端 worker 的 `RemoteSandboxRuntime`。

## 6. 可观测性

每个任务会在每个 worker 上产生两类产物:

| 路径 | 写入方 | 内容 |
|------|--------|------|
| `<ROCK_SCHEDULER_STATUS_DIR>/<task_type>_status.json` | `BaseTask.save_task_status` | 单 worker 最新状态:`task_name`、`worker_ip`、`pid`、`status`(`pending`/`running`/`success`/`failed`)、`last_run`、`error`,以及任务自定义的 `extra` 字段。 |
| `<ROCK_SCHEDULER_STATUS_DIR>/<task_type>_run_report.json` | `BaseTask.run`(由 admin 端在本轮 tick 结束后写入) | 聚合报告:总数 / 成功数 / 失败数、`success_ips` 列表、`failed_details`(`ip` + 错误堆栈)。 |

调度器内部日志会输出到 ROCK admin 标准日志路径下,logger 名称包括 `name="scheduler"`、`name="task_base"`、`name="image_clean"` 等。当设置了 `ROCK_LOGGING_PATH` 时,被调度器拉起的守护进程会把自身日志写入 `<ROCK_LOGGING_PATH>/<task>.log`(例如 `docuum.log`、`container_cleanup.log`、`image_pull.log`)。

## 7. 通过 Nacos 动态热更(可选)

当 admin 服务启用了 Nacos 配置源时,调度器会注册一个 YAML 监听器,并对配置推送做出响应:

- 仅检查 `scheduler:` 段,其他段被忽略。
- 新配置段会被计算 hash 并与上一次的 hash 比对 —— 重复推送会被自动跳过。
- 通过对新旧任务列表 diff,决定哪些任务需要安装、卸载或重新安装(`params` / `interval_seconds` / `enabled` 任意改动都会触发)。
- 被删除或重新安装的非幂等任务会先做清理:杀掉守护进程 PID 并删除状态文件。

由此,任务间隔调整、参数微调、增删任务等都可以在 admin 进程不重启的前提下生效。

## 8. 常见问题排查

| 现象 | 可能原因 | 检查项 |
|------|----------|--------|
| admin 日志输出 `Scheduler disabled, all tasks removed` | `scheduler.enabled` 为 `false` | 把 YAML 中的 `enabled` 改为 `true`。 |
| `No alive workers found for task '<type>'` | Ray 集群没有存活的 worker | 确认 `ray.nodes()` 返回的 CPU worker 处于 alive 状态;新加 worker 时可调小 `worker_cache_ttl`。 |
| 任务到点触发,但每个 worker 都进入 `failed_details` 且报连接错误 | worker 上 rocklet 未运行,或 `Port.PROXY` 被防火墙阻断 | 在 worker 上访问 rocklet 存活探针 `GET /is_alive`(例如 `curl http://<worker_ip>:<Port.PROXY>/is_alive`);若无响应,使用 `rocklet --port <Port.PROXY>` 重启或在防火墙放行该端口。 |
| 非幂等任务在某个 worker 上始终不再触发 | 状态文件中记录的 PID 仍存活 | 查看 `<status_dir>/<task>_status.json`,如 `status: running` 且 PID 仍在,则 `should_run` 会返回 `False`,属预期行为。 |
| 日志报 `Failed to create task '<class>'` | `task_class` 导入失败 | 确认对应模块在 admin 进程中可被导入(同一个 venv、`PYTHONPATH` 中可见)。 |
| `ImagePullTask` 中 `docker login` 失败 | `registry_password` 未做 base64 编码,或镜像名称解析出的仓库地址有误 | 用 `echo -n '<pwd>' \| base64` 重新编码;确认镜像名中的仓库 host 正确。 |

## 相关文档

- [Configuration](./configuration.md) —— 环境变量与运行时部署说明
- [API Documentation](../References/api.md) —— admin HTTP API
- [Python SDK Documentation](../References/Python%20SDK%20References/python_sdk.md) —— SDK 编程式使用
