# ComposeJobConfig (v2) 端到端用例：harbor + cc-proxy

本目录展示如何用 `ComposeJobConfig` v2 在 ROCK DinD 沙箱内运行 harbor 任务
（claude-code agent 跑 terminal-bench / aone-bench-java100）。

v2 的核心变化：容器编排从 job_config 内的自定义 `compose:` 块迁移到标准 `docker-compose.yaml`。
ROCK 不再解析 compose 内部结构，只负责：① 准备 DinD 外层沙箱；② 引导 dockerd；
③ `docker compose up --exit-code-from main`；④ 收退出码 + 可选 OSS 产物上传。

## 目录结构

```
examples/job/compose/
├── harbor_compose_demo.py      # ★ 开箱即用 demo（凭证走环境变量，内置所有真机 fix）
├── compose_demo.py             # 通用入口（-c 读 YAML，适合自定义 config）
├── job_config.yaml.template    # ComposeJobConfig YAML 模板（v2：只含 compose_file 指针）
├── docker-compose.yaml         # ★ 标准 compose 编排（main + proxy sidecar）
├── main.sh                     # 主容器入口脚本（harbor runner）
└── sidecars/
    └── proxy-sidecar.sh        # cc-proxy sidecar 脚本
```

两个文件的分工：

| 文件 | 负责 | 由谁解析 |
|------|------|----------|
| `job_config.yaml` | ROCK 层：job_name/timeout/labels、DinD 外层沙箱 environment、`compose_file` 指针、可选 `oss_mirror` | ROCK SDK |
| `docker-compose.yaml` | 容器层：services、depends_on、healthcheck、deploy.resources、networks、volumes、environment | **docker compose 自己**（ROCK 不解析） |

## 快速开始（推荐：harbor_compose_demo.py）

```bash
cd examples/job/compose
# 配置凭证
export ROCK_TOKEN='<your-rock-token>'
export MODEL='claude-opus-4-8'
export MODEL_BASE_URL='https://api.anthropic.com/v1'
export MODEL_API_KEY='sk-ant-...'
export OSS_BUCKET='<your-bucket>'
export OSS_ENDPOINT='oss-cn-hangzhou-internal.aliyuncs.com'
export OSS_REGION='cn-hangzhou'
export OSS_ACCESS_KEY_ID='<ak>'
export OSS_ACCESS_KEY_SECRET='<sk>'

# 运行
uv run python harbor_compose_demo.py
```

**OSS 凭证是必需的**：harbor 从 OSS 下载 dataset。AP 平台自动注入 OSS 凭证，SDK 直连模式需显式提供。

任务参数（INSTANCE_ID / DATASET / HARBOR_AGENT 等）都有默认值（对应 AP `-p`），可用环境变量覆盖，
详见 `harbor_compose_demo.py` 顶部 docstring。

## v2 运行方案

### 两层结构

```
DinD 外层沙箱（外层镜像，自带 docker 工具链）
└── runner.sh（ComposeTrial 生成，极简：dockerd 引导 + docker compose up）
    └── docker-compose.yaml（用户标准 compose 编排）
        ├── proxy service  ← main depends_on（service_started）
        └── main service   ← 决定整体退出码（约定名 main）
```

### dockerd 引导（P0）

ROCK kata 沙箱进入时没有运行 dockerd。`ComposeTrial` 生成的 `runner.sh` 会在 P0 阶段主动启动，
并内置两个 kata 环境必需的修正：

```bash
PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin \
DOCKER_IGNORE_BR_NETFILTER_ERROR=1 \
    nohup dockerd >/var/log/dockerd.log 2>&1 &
```

- **显式 PATH**：`nohup` 启动的 dockerd 不继承交互 shell 的 PATH，否则报 `containerd executable file not found`。
- **`DOCKER_IGNORE_BR_NETFILTER_ERROR=1`**：kata guest 缺 `/proc/sys/net/bridge/bridge-nf-call-iptables`，
  否则 bridge 网络初始化失败。

### compose 编排要点

- **service 名 = DNS 名**：`proxy` service 在同一 compose network 里直接用 `http://proxy:8082` 访问。
- **依赖编排**：main `depends_on: proxy: condition: service_started`，无需 runner.sh 里的 busybox nc 探测逻辑。
- **资源限制**：`deploy.resources.limits` 原生表达，外层 cpus/memory ≥ 内层各容器之和。
- **凭证注入**：`environment.env` 注入外层沙箱，compose 文件里用 `${VAR}` 插值传给内层 service。

> ⚠️ **healthcheck + service_healthy 的陷阱（真机验证踩坑）**：本例 proxy-sidecar.sh 会**按 agent 类型自门控**——
> 对 `agent=claude-code` 它打印 `Proxy not needed ... sleeping` 后直接 sleep，**不监听 8082 端口**。
> 若给 proxy 配端口 healthcheck + main `depends_on: service_healthy`，proxy 永远不健康 →
> main **根本不启动** → `docker compose up` 返回 rc=1。
> 因此本例改用 `condition: service_started`（只等 proxy 容器起来，不等"健康"）。
> 经验：**只有当 sidecar 真的会进入监听态时才用 service_healthy**；自门控/可空跑的 sidecar 用 service_started。

### 沙箱内文件布局

uploads 后的路径（由 `environment.uploads` 配置）：

```
/rock/compose/docker-compose.yaml   ← 来自本地 docker-compose.yaml
/rock/compose/main.sh               ← 来自本地 main.sh
/rock/compose/sidecars/             ← 来自本地 sidecars/
/rock/runner.sh                     ← ComposeTrial 生成（极简模板）
/rock/logs/compose.log              ← runner.sh trap EXIT 收集的 docker compose logs
/rock/logs/up.log                   ← docker compose up 实时输出（tee）
```

## 使用步骤（compose_demo.py + YAML）

### 1. 准备配置文件

```bash
cp examples/job/compose/job_config.yaml.template examples/job/compose/job_config.yaml
# 编辑 job_config.yaml，填入真实镜像名和占位符
```

主要需要替换的占位符：

| 占位符 | 说明 |
|--------|------|
| `<ROCK_TOKEN>` | ROCK 集群认证 token |
| `<HARBOR_MAIN_IMAGE>` | harbor runner 镜像（含 harbor CLI + claude-code + docker 工具链） |
| `<CC_PROXY_IMAGE>` | claude-code proxy 镜像 |
| `<MODEL>` | 模型名，e.g. `claude-opus-4-8` |
| `<MODEL_API_KEY>` | 模型 API Key |
| `<MODEL_BASE_URL>` | 模型 Base URL |
| `<INSTANCE_ID>` | 任务 ID，e.g. `mailman` |
| `<OSS_*>` | OSS 凭证和 bucket 信息 |

### 2. 运行

```bash
python examples/job/compose/compose_demo.py -c examples/job/compose/job_config.yaml
```

## v1 → v2 迁移对照

| v1 位置 | v2 去向 |
|---------|---------|
| 顶层 `script_path` | 删除；改写为 `docker-compose.yaml` 里 main service 的 `command` |
| `compose.main` | `services.main` |
| `compose.init_containers[]` | `services.<x>` + main `depends_on.<x>: service_completed_successfully` |
| `compose.sidecars[]` | `services.<x>`（监听态 sidecar 用 `healthcheck` + `depends_on: service_healthy`；自门控/可空跑的用 `service_started`） |
| `*.resources` | `services.<x>.deploy.resources` |
| `*.secret_env` | `services.<x>.environment`（值用 `${VAR}` 从外层 env 插值） |
| 新增 | `job_config.yaml` 顶层加 `compose_file: ./docker-compose.yaml` |

## 与 HarborJobConfig 的对比

本示例展示 ComposeJobConfig 的表达能力。实际上，对于"调 harbor CLI 跑 benchmark"的场景，
`HarborJobConfig` 更原生（自带 agents/datasets/verifier 结构化支持）。

ComposeJobConfig v2 更适合：自己掌控每个容器镜像和脚本、用标准 compose 语义表达复杂编排
（init 依赖、healthcheck、资源限制、共享卷、多 network 等）。
