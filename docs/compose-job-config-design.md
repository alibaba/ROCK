# ComposeJobConfig 设计方案（v2 · 标准 docker-compose）

> 为 ROCK SDK 设计支持多容器场景的 `ComposeJobConfig`。
> **v2 重构核心**：放弃 v1 的自定义 `compose:` 块，改为
> **`job_config.yaml`（job 元信息 + DinD 外层沙箱）+ 用户标准 `docker-compose.yaml`（容器编排）** 的双文件方式。
> 容器编排完全回归 Docker Compose 原生语义，ROCK 不再重新发明 init/sidecar/resources/secret 的声明式字段。
>
> 本文档为设计方案，**不含实现代码**。

---

## 0. 设计概要（TL;DR）

| 决策点 | v1（旧） | v2（本方案） |
|--------|---------|-------------|
| 容器编排载体 | job_config.yaml 内自定义 `compose:` 块 | **用户独立的标准 `docker-compose.yaml`** |
| 编排执行 | ComposeTrial 渲染 runner.sh，手写 `docker run` 串编排 | **`docker compose up`** 原生编排 |
| 主容器标识 | service 名固定 `main` + `--network-alias main` | **service 名硬约定为 `main`**，`--exit-code-from main` 取退出码 |
| init 容器 | `compose.init_containers[]` + runner Phase 2 串行 | **compose `depends_on: service_completed_successfully`** 原生 |
| sidecar | `compose.sidecars[]` + runner Phase 3 后台 | **compose 普通 service** + `depends_on: service_healthy` |
| 资源限制 | `ResourceSpec`（自定义四字段） | **compose `deploy.resources`** 原生 |
| secret 注入 | `secret_env`（K8s 风格自定义） | **compose `environment` / `env_file`** 原生 |
| OSS 依赖下载 | `compose.main.oss_deps[]` | **compose 里写成 init service**（用户自己拉），或主容器脚本内拉 |
| 健康探测 | `HealthSpec` + runner Phase 4 busybox nc | **compose `healthcheck`** 原生 |
| 容器间网络 | runner 建 `docker network` + `--network-alias` | **compose 默认 network**（service 名即 DNS 名） |
| 共享卷 | runner 建命名卷 + `main_mount_path` 逻辑映射 | **compose `volumes`** 原生 |
| OSS 产物上传 | `environment.oss_mirror`（复用） | **`environment.oss_mirror`（复用，唯一保留的 ROCK 收尾扩展）** |
| 类型检测特征 | `"compose" in data` | **`"compose_file" in data`**（顶层字符串指针） |
| Trial | ComposeTrial 渲染长 runner.sh | **ComposeTrial 上传 compose 文件 + 极简 runner.sh（仅 dockerd 引导 + `docker compose up`）** |

**为什么换 v2**：v1 把 docker-compose 已经标准化的概念（depends_on / healthcheck / deploy.resources / networks / volumes）用自定义 pydantic 字段重新发明了一遍，既增加学习成本，又让 ComposeTrial 背上几百行 runner.sh 渲染逻辑。v2 让用户直接写他们已经熟悉的 `docker-compose.yaml`，ROCK 只负责：① 准备 DinD 外层沙箱；② 引导 dockerd；③ `docker compose up`；④ 收退出码 + 可选 OSS 产物上传。

核心层次图：

```
DinD 外层沙箱  ← environment (SandboxConfig): image=docker:dind, memory, cpus
└── runner.sh （ComposeTrial 生成，极简）
    ├── P0: 引导并等待 dockerd 就绪
    ├── P1: docker compose -f docker-compose.yaml up --exit-code-from main --abort-on-container-exit
    │        └── compose 原生编排：
    │            ├── init services    ← depends_on: service_completed_successfully
    │            ├── sidecar services ← depends_on: service_healthy
    │            └── main service     ← 决定整体退出码（约定名 main）
    └── P2: 收尾——docker compose logs / down，可选 oss_mirror 产物上传
```

---

## 1. 用户目录结构

```
my-compose-job/
├── job_config.yaml          # ROCK job 元信息 + DinD 外层沙箱（必须）
├── docker-compose.yaml      # 标准 docker-compose 编排（必须，由 compose_file 指向）
├── main.sh                  # 主容器入口脚本（被 compose 的 main service 引用）
├── init/
│   └── dependency-init.sh   # init service 脚本
└── sidecars/
    └── proxy-sidecar.sh      # sidecar service 脚本
```

两个文件的分工：

| 文件 | 负责 | 由谁解析 |
|------|------|----------|
| `job_config.yaml` | ROCK 层：job_name/timeout/labels、**DinD 外层沙箱** environment、`compose_file` 指针、可选 `oss_mirror` | ROCK SDK（pydantic `ComposeJobConfig`） |
| `docker-compose.yaml` | 容器层：services、depends_on、healthcheck、deploy.resources、networks、volumes、environment/env_file | **docker compose 自己**（ROCK 不解析其内容） |

> **关键原则**：ROCK **不解析 `docker-compose.yaml` 的内部结构**，只把它当作一个待上传、待 `docker compose up` 的文件。这意味着用户能用 compose 的全部能力（profiles、多 network、configs、secrets…），ROCK 不构成表达力上限。

---

## 2. `job_config.yaml` 示例

继承 BashJobConfig 的 environment 描述外层沙箱；**移除** v1 的 `compose:` 块和顶层 `script_path`（入口已在 compose 的 main service 里），新增 `compose_file` 指针。

```yaml
# ComposeJobConfig 示例 — claude-code-swe 多容器场景

# ── 继承自 JobConfig ────────────────────────────────────────────
job_name: claude-code-swe-2026-06-09
namespace: xrl-sandbox
experiment_id: exp-swe-bench-001
timeout: 7200                       # 整体超时（docker compose up 全生命周期）
labels:
  team: xrl
  task: swe-bench

# ── ComposeJobConfig 专有：指向标准 compose 文件（本地相对路径）──
compose_file: ./docker-compose.yaml

# ── 继承自 EnvironmentConfig（描述 DinD 外层沙箱）──────────────
environment:
  image: docker:27-dind            # ★ 外层沙箱镜像必须是 DinD
  memory: "32g"                    # 外层沙箱总内存（须 ≥ 内层各容器之和）
  cpus: 8                          # 外层沙箱总 CPU
  cluster: default
  startup_timeout: 1800
  use_kata_runtime: false

  # 把 compose 文件 + 脚本目录送进沙箱（compose 文件里用相对/绝对路径引用）
  uploads:
    - ["./docker-compose.yaml", "/rock/compose/docker-compose.yaml"]
    - ["./main.sh", "/rock/compose/main.sh"]
    - ["./init", "/rock/compose/init"]
    - ["./sidecars", "/rock/compose/sidecars"]

  # 沙箱级环境变量（注入外层，docker compose 可读，用于 compose 内 ${VAR} 插值）
  env:
    OSS_BUCKET: xrl-artifacts
    OSS_ENDPOINT: oss-cn-hangzhou-internal.aliyuncs.com
    OSS_ACCESS_KEY_ID: "<from-secret-or-host-env>"
    OSS_ACCESS_KEY_SECRET: "<from-secret-or-host-env>"

  # 产物上传（复用现有机制，compose 结束后由 runner P2 执行）
  oss_mirror:
    enabled: true
    oss_bucket: xrl-artifacts
```

要点：

- **没有 `script_path`**：主容器入口完全由 `docker-compose.yaml` 的 main service（`command:` / `entrypoint:` / 镜像默认）决定。
- **`compose_file`** 是本地相对路径，ROCK 会把它 upload 到沙箱并在 `docker compose -f <沙箱内路径>` 中引用（见 §4）。
- **`environment.env`** 注入到外层沙箱，`docker compose` 执行时这些值可用于 compose 文件里的 `${VAR}` 插值——这是把外层凭证传给内层 service 的标准通道。

---

## 3. `docker-compose.yaml` 示例（标准语义）

这是用户写的**纯标准 compose 文件**，ROCK 不解析。下面示例展示如何用原生 compose 表达 v1 那套 init/sidecar/main/资源/健康/共享卷。

```yaml
# docker-compose.yaml — claude-code-swe 场景（纯标准 compose）
name: rock-compose-swe

networks:
  default:
    name: rock_compose                # service 名即 DNS 名，main 用 http://proxy:8082 访问

volumes:
  shared:                             # 跨容器共享卷（替代 v1 的命名卷 + main_mount_path）

services:
  # ── init service：主容器前置依赖，跑完即退（service_completed_successfully）──
  dependency-init:
    image: code-agi-registry/claude-code-swe:20260508
    command: ["bash", "/rock/compose/init/dependency-init.sh"]
    volumes:
      - shared:/var/lib/dependency
      - /rock/compose:/rock/compose:ro
    environment:
      OSS_BUCKET: "${OSS_BUCKET}"      # 来自外层沙箱 environment.env，compose 插值注入
      OSS_ACCESS_KEY_ID: "${OSS_ACCESS_KEY_ID}"
      OSS_ACCESS_KEY_SECRET: "${OSS_ACCESS_KEY_SECRET}"

  # ── sidecar service：与 main 并行；service 名 proxy 即 network-alias ──
  proxy:
    image: agent-platform-registry/claude-code-proxy:latest
    command: ["bash", "/rock/compose/sidecars/proxy-sidecar.sh"]
    volumes:
      - /rock/compose:/rock/compose:ro
    deploy:
      resources:
        limits:
          cpus: "1"
          memory: 1g
    healthcheck:                       # 原生健康探测，替代 v1 HealthSpec
      test: ["CMD", "nc", "-z", "localhost", "8082"]
      interval: 5s
      timeout: 3s
      retries: 12                      # ≈ 60s 就绪窗口

  # ── main service：约定名 main，决定整体退出码 ──
  main:
    image: code-agi-registry/claude-code-swe:20260508
    command: ["bash", "/rock/compose/main.sh"]
    depends_on:
      dependency-init:
        condition: service_completed_successfully  # init 跑完才启动 main
      proxy:
        condition: service_healthy                 # proxy 就绪才启动 main
    volumes:
      - shared:/var/lib/dependency:ro              # 读 init 写入的共享卷
      - /rock/compose:/rock/compose:ro
    deploy:
      resources:
        limits:
          cpus: "4"
          memory: 16g
    environment:
      DATASET: princeton-nlp/SWE-bench_Verified
      SPLIT: test
      MODEL: claude-opus-4-8
      ANTHROPIC_BASE_URL: http://proxy:8082        # service 名做主机名
      ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"    # 从外层 env / host env 插值
```

**v1 自定义字段 → v2 标准 compose 的映射**（用户写 compose 时照此翻译）：

| v1 ComposeJobConfig 字段 | v2 docker-compose.yaml |
|--------------------------|------------------------|
| `compose.main` | `services.main` |
| 顶层 `script_path: ./main.sh` | `services.main.command: ["bash", "/rock/compose/main.sh"]` |
| `compose.init_containers[]` | `services.<x>` + `main.depends_on.<x>.condition: service_completed_successfully` |
| `compose.sidecars[]` | `services.<x>`（普通 service） |
| `SidecarSpec.health` | `services.<x>.healthcheck` + `main.depends_on.<x>.condition: service_healthy` |
| `ResourceSpec.cpus/memory/*_limit` | `services.<x>.deploy.resources.{reservations,limits}` |
| `secret_env`（K8s 风格） | `services.<x>.environment` / `env_file`（值用 `${VAR}` 从外层注入） |
| `OssDep.oss_deps[]` | 写成 init service 拉取，或主容器脚本内拉（见 §6.1） |
| `VolumeMount` + `main_mount_path` | 标准命名 `volumes:` + 各 service 自己的 `volumes:` 挂载点 |
| `--network-alias <name>` | compose 默认 network 下 service 名即 DNS 名（零配置） |
| `command` / `args` / `privileged` | `services.<x>.command` / `entrypoint` / `privileged: true` |

---

## 4. ComposeJobConfig 数据模型（Python）

v2 模型大幅瘦身——不再有 ResourceSpec / VolumeMount / SecretEnvEntry / OssDep / HealthSpec / 各 ContainerSpec / ComposeSpec。只剩一个顶层 `compose_file` 字符串。

```python
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import ConfigDict, Field, model_validator

from rock.sdk.job.config import JobConfig


class ComposeJobConfig(JobConfig):
    """Docker Compose 多容器 Job 配置（v2：标准 compose 文件）。

    与 BashJobConfig 平级，直接继承 JobConfig。不再有顶层 script/script_path——
    主容器入口由 docker-compose.yaml 的 main service 决定。

    类型检测特征：YAML 中存在 ``compose_file`` 键即识别为本类型。
    """

    model_config = ConfigDict(extra="forbid")

    job_name: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d__%H-%M-%S"))

    # 必填，存在即标识 ComposeJobConfig。本地路径（相对 job_config.yaml）。
    compose_file: str

    # 任一容器退出即停止整组（docker compose up --abort-on-container-exit）。
    # 默认开启；置 false 可让 sidecar 崩溃不阻断 main。
    abort_on_container_exit: bool = True

    @model_validator(mode="after")
    def _validate_compose_file(self) -> "ComposeJobConfig":
        # 仅做存在性 / 后缀的轻量校验；不解析 compose 内部结构。
        if not self.compose_file:
            raise ValueError("compose_file 不能为空")
        return self
```

> **主 service 名硬约定为 `main`**：runner 用 `--exit-code-from main` 取退出码（见 §5），不引入可配置字段，保持约定优先、零配置。
> **`abort_on_container_exit`**：默认开启（任一 service 退出即收敛整组）；需要"sidecar 崩溃不阻断 main"时显式置 `false`，runner 据此决定是否给 `docker compose up` 加 `--abort-on-container-exit`（见 §6）。

### `JobConfig.from_yaml()` 检测分支（修改）

检测顺序：**Harbor → Compose → Bash**。特征字段从 `"compose"` 改为 **`"compose_file"`**。

```python
# JobConfig.from_yaml() 内（lazy import 避免循环依赖）
# 1) Harbor：特征是 required experiment_id
try:
    return HarborJobConfig.model_validate(data)
except (ValidationError, ValueError) as exc:
    harbor_error = exc

# 2) Compose：特征是存在 "compose_file" 键（v2 变更点）
if "compose_file" in data:
    from rock.sdk.job.compose.config import ComposeJobConfig
    try:
        return ComposeJobConfig.model_validate(data)
    except (ValidationError, ValueError) as exc:
        compose_error = exc

# 3) Bash：兜底
try:
    return BashJobConfig.model_validate(data)
except (ValidationError, ValueError) as exc:
    bash_error = exc
```

**向后兼容**：现有 Bash/Harbor YAML 无 `compose_file` 键，零影响；含 `compose_file` 的 YAML 因 Bash/Harbor 的 `extra="forbid"` 被它们拒绝，唯一落到 ComposeJobConfig。

---

## 5. ComposeTrial 逻辑设计

继承 `AbstractTrial`，三段式接口，末尾 `register_trial(ComposeJobConfig, ComposeTrial)`。相比 v1，runner.sh 从几百行的多 Phase 编排**缩到只剩 dockerd 引导 + `docker compose up`**。

```python
class ComposeTrial(AbstractTrial):
    """在 DinD 沙箱内用 `docker compose` 编排多容器的 Trial。

    setup()  → 上传 compose 文件 + 脚本 + 凭证，生成极简 runner.sh
    build()  → 返回 "bash /rock/runner.sh"
    collect()→ runner.sh 退出码 = main service 退出码（--exit-code-from main）
    """
    _config: ComposeJobConfig
```

### 5.1 `on_sandbox_ready()`

调 `super().on_sandbox_ready(sandbox)` 回填 `namespace` / `experiment_id`。把 **OSS 凭证**解析后写入 `environment.env`，供 ① `docker compose` 插值 ② P2 产物上传使用（同 BashTrial 思路）。

### 5.2 `setup()`

```python
async def setup(self, sandbox: Sandbox) -> None:
    await self._upload_files(sandbox)              # 上传 environment.uploads（含 compose 文件 + 脚本）

    runner = self._render_runner_sh()              # 极简模板，几乎无条件渲染
    await sandbox.fs.write_text("/rock/runner.sh", runner)
    await sandbox.arun("chmod +x /rock/runner.sh")
```

不再需要 `_materialize_inline_scripts` / `ensure_ossutil` / 各容器 docker run 渲染——这些要么交给 compose，要么交给用户脚本。

### 5.3 `build()`

```python
def build(self) -> str:
    return "bash /rock/runner.sh"
```

### 5.4 `collect()`

```python
async def collect(self, sandbox, output, exit_code) -> TrialResult:
    exc = None
    if exit_code != 0:
        exc = ExceptionInfo(
            exception_type="ComposeMainServiceFailed",
            exception_message=f"main service exited with {exit_code}",
        )
    # 各 service 日志由 runner P2 `docker compose logs` 落到 /rock/logs/compose.log
    obs = await sandbox.arun("cat /rock/logs/compose.log 2>/dev/null || true")
    return TrialResult(
        task_name=self._config.job_name or "",
        exception_info=exc,
        raw_output=output,
        exit_code=exit_code,
    )
```

---

## 6. runner.sh 生命周期（v2 极简版）

runner.sh 在 **DinD 外层沙箱** 内运行；退出码 = main service 退出码。整体只有 3 个 Phase。

```bash
#!/bin/bash
# runner.sh — ROCK ComposeJob 运行时（v2：委托 docker compose）
set -uo pipefail
COMPOSE_FILE="/rock/compose/docker-compose.yaml"
LOG_DIR="/rock/logs"; mkdir -p "$LOG_DIR"
RUNNER_EXIT=0

cleanup_all() {
  docker compose -f "$COMPOSE_FILE" logs --no-color > "$LOG_DIR/compose.log" 2>&1 || true
  docker compose -f "$COMPOSE_FILE" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup_all EXIT
trap 'RUNNER_EXIT=143; exit 143' TERM INT

# ── P0: 引导并等待 dockerd 就绪 ──
echo "[runner] P0: wait docker daemon"
if ! docker info >/dev/null 2>&1; then
  if ! pgrep -x dockerd >/dev/null 2>&1; then
    PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin \
    DOCKER_IGNORE_BR_NETFILTER_ERROR=1 nohup dockerd >/var/log/dockerd.log 2>&1 &
  fi
fi
for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
docker info >/dev/null 2>&1 || { echo "docker daemon not ready"; exit 1; }

# 可选：私有 registry 登录（凭证来自 SandboxConfig.registry_*）
if [ -n "${REGISTRY_USERNAME:-}" ]; then
  docker login "${REGISTRY_HOST:-}" -u "$REGISTRY_USERNAME" -p "$REGISTRY_PASSWORD" >/dev/null 2>&1 || true
fi

# ── P1: docker compose up，主 service 退出即收敛 ──
# __ABORT_FLAG__ 由 ComposeTrial 渲染：abort_on_container_exit=True →
# "--abort-on-container-exit"，否则为空字符串。
echo "[runner] P1: docker compose up"
docker compose -f "$COMPOSE_FILE" up \
  __ABORT_FLAG__ \
  --exit-code-from main 2>&1 | tee "$LOG_DIR/up.log"
RUNNER_EXIT=${PIPESTATUS[0]}
echo "[runner] main service exited rc=$RUNNER_EXIT"

# ── P2: 可选 OSS 产物上传（仅当 environment.oss_mirror.enabled）──
__PHASE2_OSS_UPLOAD__

exit "$RUNNER_EXIT"
```

各 Phase 要点：

| Phase | 行为 | 失败语义 |
|-------|------|----------|
| 0 | 引导 dockerd、等就绪、可选私有 registry 登录 | dockerd 60s 未就绪 → exit 1 |
| 1 | `docker compose up [--abort-on-container-exit] --exit-code-from main` | main service 退出码即整体结果；`abort_on_container_exit=True`（默认）时任一容器退出触发 abort |
| 2 | 可选 OSS 产物上传（ossutil cp） | 收尾失败不改变 RUNNER_EXIT |
| EXIT(trap) | `docker compose logs` 落盘 + `docker compose down -v` 清理 | 始终执行 |

> `--abort-on-container-exit` 让任一 service 退出即停止整组；`--exit-code-from main` 保证整体退出码取自 main service。init service 跑完会"退出"，但因为它是 main 的 `depends_on: service_completed_successfully` 前置、在 main 启动前就结束，配合 compose 的依赖编排不会误触 abort 主流程——这是标准 compose 行为，无需 ROCK 干预。

---

## 7. 关键机制（v2：几乎全部委托 compose）

### 7.1 容器间网络
compose 默认建一个 project network，**service 名即 DNS 名**。main 直接 `http://proxy:8082` 访问 proxy service。零配置，无需 runner 建 network / `--network-alias`。

### 7.2 共享卷
compose 顶层 `volumes:` 声明命名卷，各 service 在自己的 `volumes:` 里挂到各自路径。v1 的 `main_mount_path` 逻辑映射消失——init 写 `/var/lib/dependency`、main 读 `/var/lib/dependency:ro`，各 service 挂载点本就独立，是 compose 原生能力。

### 7.3 secret / 凭证注入
两级：
1. **外层 → compose 插值**：`environment.env` 注入外层沙箱，`docker-compose.yaml` 里用 `${VAR}` 引用，compose 执行时插值。
2. **service 级**：compose 的 `environment:` / `env_file:` 直接写。
**安全边界**：仍仅防 YAML 明文（值会出现在容器 env / `docker inspect`），不防沙箱内进程读取。需更强隔离用 compose `secrets:` + 文件挂载。

### 7.4 资源限制
compose `deploy.resources.{limits,reservations}.{cpus,memory}` 原生表达。外层 `environment.cpus/memory` 是总上限，内层之和须 ≤ 外层——v2 **不再做 Python 侧 `_resource_budget_check`**（ROCK 不解析 compose 内容），由用户自行保证；如需校验可作为后续可选增强（解析 compose 求和）。

### 7.5 DinD 边界问题
- **daemon 就绪**：P0 引导 + 轮询 `docker info`（kata 后端 dockerd 不自启，需 ROCK 引导，见 P0 注释）。
- **私有镜像**：P0 `docker login`，凭证来自 `SandboxConfig.registry_username/password`。
- **容器清理**：`trap EXIT → docker compose down -v --remove-orphans`，比 v1 按 `$$` 过滤 `docker rm` 更干净。

### 7.6 init / 依赖编排
完全交给 compose `depends_on` 的 condition：
- `service_completed_successfully` — init 跑完且成功才启动依赖方；init 失败则 compose 直接报错、main 不启动。
- `service_healthy` — sidecar healthcheck 通过才启动 main，替代 v1 的 busybox nc 探测。

---

## 8. 三类 Job 对比表（v2）

| 维度 | BashJobConfig | HarborJobConfig | ComposeJobConfig (v2) |
|------|--------------|----------------|------------------|
| 容器数量 | 1（沙箱内直跑脚本） | 1 主 + Harbor 内部编排 | N services，DinD 内 `docker compose` 编排 |
| 编排载体 | 顶层 `script_path` | `harbor jobs start -c` | **独立 `docker-compose.yaml`** |
| 主容器入口 | 顶层 `script_path` | harbor CLI | compose **main service**（约定名） |
| 外层沙箱 | `environment` | `environment`(Rock 扩展) | `environment`，**image 须为 dind** |
| 内层容器资源 | 无 | Harbor override | **compose `deploy.resources`** |
| OSS 产物上传 | `environment.oss_mirror` | `environment.oss_mirror` | `environment.oss_mirror`（复用） |
| OSS 依赖下载 | 无内建 | `environment.oss_deps` | **compose init service / 主容器脚本** |
| Secret 注入 | `environment.env`(明文) | `environment.env` | **compose `environment`/`env_file`** + 外层 `${VAR}` 插值 |
| init / 依赖 | 无 | Harbor 内部 | **compose `depends_on` condition** |
| 健康探测 | 无 | Harbor 内部 | **compose `healthcheck`** |
| YAML 特征字段 | 无（兜底） | `experiment_id`(required) | **`compose_file`(存在即识别)** |
| Trial 类 | `BashTrial` | `HarborTrial` | `ComposeTrial`（极简 runner） |
| 沙箱内执行 | `sandbox.nohup(script)` | harbor CLI | **`docker compose up --exit-code-from main`** |
| dockerd 依赖 | ❌ | ✅ | ✅ |
| collect 产物 | `TrialResult` | `list[TrialResult]` | `TrialResult`（+`docker compose logs`） |

---

## 9. 集成与边界问题清单（v2）

| # | 问题 | 处理 |
|---|------|------|
| 1 | `from_yaml` 检测顺序 | Harbor → Compose(`"compose_file" in data`) → Bash；三者 extra=forbid 互斥 |
| 2 | Registry 注册 | `register_trial(ComposeJobConfig, ComposeTrial)`；确保 `compose/trial.py` 被 import 链触发 |
| 3 | timeout 语义 | `JobConfig.timeout` = 整体超时（`docker compose up` 全程）；service 级超时用 compose `healthcheck`/脚本自管 |
| 4 | 主 service 识别 | 硬约定为 `main`；`--exit-code-from main` 取退出码，无可配置字段 |
| 5 | compose 文件上传 | 通过 `environment.uploads` 显式上传 compose 文件 + 脚本到 `/rock/compose/`；runner `-f` 引用沙箱内路径 |
| 6 | compose 文件内部 | ROCK **不解析**；表达力 = docker compose 全集；校验交给 `docker compose config`（可选 setup 阶段预检） |
| 7 | init 失败 | compose `service_completed_successfully` 失败 → compose 报错，main 不启动，runner 非 0 退出 |
| 8 | sidecar 崩溃 | 默认 `abort_on_container_exit=True` 让整组停止；需 sidecar 崩溃不阻断 main 时置 `false`，runner 去掉 `--abort-on-container-exit`（此时 main 跑完仍正常收敛） |
| 9 | 两层资源混淆 | v2 不在 Python 侧求和校验（不解析 compose）；文档提示用户外层 ≥ 内层之和 |
| 10 | 向后兼容 | 现有 Bash/Harbor YAML 无 `compose_file` 键，零影响 |
| 11 | secret 安全边界 | 文档明确：仅防 YAML 明文，不防沙箱内读取；更强隔离用 compose `secrets:` |
| 12 | 内联脚本 | v2 不再支持 job_config 内联 script；脚本走 uploads + compose `command` 引用，路径统一 `/rock/compose/` |

---

## 10. 从 v1 迁移指南

已按 v1 写过 `compose:` 块的用户，按下表把单文件拆成 `job_config.yaml` + `docker-compose.yaml`：

| v1 位置 | v2 去向 |
|---------|---------|
| 顶层 `job_name/namespace/experiment_id/timeout/labels` | 留在 `job_config.yaml`（不变） |
| 顶层 `script_path` | 删除；改写为 `docker-compose.yaml` 里 main service 的 `command` |
| `environment.*`（外层沙箱） | 留在 `job_config.yaml`（不变），uploads 增加 compose 文件 |
| `environment.oss_mirror` | 留在 `job_config.yaml`（不变） |
| `compose.main` | `docker-compose.yaml` → `services.main` |
| `compose.init_containers[]` | `services.<x>` + main `depends_on.<x>: service_completed_successfully` |
| `compose.sidecars[]` | `services.<x>`（+ `healthcheck` + main `depends_on.<x>: service_healthy`） |
| `*.resources` | `services.<x>.deploy.resources` |
| `*.secret_env` | `services.<x>.environment`（值用 `${VAR}` 从外层 env 插值） |
| `main.oss_deps[]` | 写成 init service 拉取，或 main 脚本内拉 |
| `volume_mounts` + `main_mount_path` | 顶层 `volumes:` + 各 service `volumes:` 挂载点 |
| 新增 | `job_config.yaml` 顶层加 `compose_file: ./docker-compose.yaml` |

---

## 11. 后续实现建议（非本设计范围）

1. **改造模块**：`rock/sdk/job/compose/{config.py, trial.py}` —— config 删掉 v1 的全部子模型，trial 删掉 runner 多 Phase 渲染逻辑，runner.sh 模板换成 §6 极简版。
2. **TDD**：
   - `from_yaml` 三类型识别参数化测试（特征字段改 `compose_file`）；
   - `ComposeJobConfig` 校验测试（`compose_file` 必填、`abort_on_container_exit` 默认 True、extra forbid）；
   - runner.sh 渲染快照测试（`ABORT_FLAG` / OSS_UPLOAD 两个占位符）。
3. **集成测试**：标 `@pytest.mark.need_admin`，在真实 DinD 沙箱内跑一个最小 `main + proxy(healthcheck)` compose 用例，验证 `--exit-code-from main` 退出码透传。
4. **可选增强**：
   - setup 阶段 `docker compose config -q` 预检 compose 文件合法性（fail-fast）；
   - 解析 compose 求 `deploy.resources` 之和，与外层资源比较给 warning（恢复 v1 的 budget check，但作为可选项）。
