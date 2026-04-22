# Sandbox Timezone — Requirement Spec

## Background

ROCK 的 Docker sandbox 启动流程此前只向容器传递了 `ROCK_TIME_ZONE`，该变量供 ROCK 自身日志和调度逻辑使用（需要 IANA 时区名，如 `Asia/Shanghai`），但并未设置容器内的系统时区。这导致容器内的系统时区始终为 UTC，产生两个具体问题：

1. **文件修改时间偏差**：sandbox 内创建或修改的文件，其 `mtime` 按 UTC 记录。前端展示文件列表时，用户看到的修改时间与本地实际时间存在时差（东八区场景下差 8 小时）。
2. **系统命令时间不一致**：`date`、`ls -l` 等系统命令输出 UTC 时间，与用户预期不符。

本次修复目标是：
- 让 sandbox 内的系统时区跟随宿主机配置，使文件修改时间和系统命令输出与用户所在时区一致
- 前端展示文件信息时不再有时差偏差
- 在镜像来源多样、不可控的前提下保持可用
- 不要求业务镜像预装 `tzdata`

---

## Solution

在宿主机上确保安装 `tzdata`（`/usr/share/zoneinfo/` 目录存在），根据 `ROCK_TIME_ZONE` 环境变量（默认 `Asia/Shanghai`）定位对应的 zoneinfo 文件，通过 `docker run -v` 以只读方式挂载到容器的 `/etc/localtime`。

### 原理

Linux C 库（glibc / musl）解析系统时区的优先级：

1. `TZ` 环境变量（如果设置了）
2. `/etc/localtime`（TZif 二进制文件）
3. 回退 UTC

通过 bind mount 将宿主机的 zoneinfo 文件挂载到容器 `/etc/localtime`，Docker bind mount 会直接遮盖（shadow）容器内原有的 `/etc/localtime` 文件，无论容器镜像是否自带该文件。挂载后容器内的系统命令（`date`、`ls -l`、`stat`）均会按照挂载的时区文件解析时间。

### 兼容性

**容器侧**：bind mount `/etc/localtime` 后，glibc（Ubuntu/Debian/CentOS/RHEL）和 musl（Alpine）均能正确读取 TZif 文件，覆盖绝大多数 Linux 容器镜像。

**宿主机侧**：所有主流 Linux 发行版（Ubuntu、Debian、CentOS、RHEL、Amazon Linux）默认安装 `tzdata`，zoneinfo 路径统一为 `/usr/share/zoneinfo/`。运维侧保证宿主机有 `tzdata` 即可。

---

## In / Out

### In（本次要做的）

1. **Docker sandbox 启动时挂载 zoneinfo 文件到容器 `/etc/localtime`**
   - 根据 `ROCK_TIME_ZONE`（默认 `Asia/Shanghai`）定位 `/usr/share/zoneinfo/{ROCK_TIME_ZONE}`
   - 以只读方式挂载：`-v /usr/share/zoneinfo/{tz}:/etc/localtime:ro`
   - 启动前校验文件是否存在，不存在则 warning 并跳过挂载

2. **保持现有 `ROCK_TIME_ZONE` 行为不变**
   - `ROCK_TIME_ZONE` 使用 IANA 时区名（如 `Asia/Shanghai`），供 ROCK 日志、调度器、时间戳格式化等 Python 应用层逻辑使用
   - 该变量继续通过 `-e` 传入容器

3. **让容器内文件时间和系统命令与用户时区一致**
   - `date`、`ls -l` 等命令按挂载的时区显示本地时间
   - 文件 `mtime` 的展示格式与用户预期时区一致
   - 前端读取文件修改时间时不再出现时差偏差

### Out（本次不做的）

- 不修改镜像内容
- 不要求所有业务镜像安装 `tzdata`
- 不向容器传递 `TZ` 环境变量（依靠 `/etc/localtime` 生效）
- 不修改 ROCK 内部 `ROCK_TIME_ZONE` 的默认值
- 不新增 ROCK 环境变量

---

## Acceptance Criteria

- **AC1**：Docker sandbox 启动时，`docker run` 包含 `-v /usr/share/zoneinfo/{tz}:/etc/localtime:ro` 挂载
- **AC2**：`{tz}` 的值取自 `ROCK_TIME_ZONE`，默认 `Asia/Shanghai`
- **AC3**：挂载后容器内 `date` 命令输出对应时区的本地时间
- **AC4**：当宿主机上对应的 zoneinfo 文件不存在时，打印 warning 日志并跳过挂载，不阻断启动
- **AC5**：现有 `ROCK_TIME_ZONE` 行为不变（IANA 格式，供 Python 应用层使用）

---

## Constraints

- 不引入新的 Python 依赖
- 不要求修改用户镜像 Dockerfile
- 不修改 sandbox 启动 API 的对外字段
- 宿主机需安装 `tzdata`（`/usr/share/zoneinfo/` 目录存在）

---

## Risks & Rollout

- **风险**：宿主机未安装 `tzdata` 时挂载无效 — 通过 AC4 的 warning + 跳过机制缓解
- **回滚**：仅涉及 `rock/deployments/docker.py`，回滚成本低
- **上线策略**：无数据库变更，无协议破坏，可直接随 admin / deployment 代码发布
