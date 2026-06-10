# ComposeJobConfig 端到端用例：harbor + cc-proxy

本目录展示如何用 `ComposeJobConfig` 在 ROCK DinD 沙箱内运行 harbor 任务
（claude-code agent 跑 terminal-bench / aone-bench-java100）。

## 目录结构

```
examples/job/compose/
├── harbor_compose_demo.py      # ★ 开箱即用 demo（凭证走环境变量，内置所有真机 fix）
├── .env.example                # 凭证模板（cp 成 .env 填值后 source）
├── compose_demo.py             # 通用入口（-c 读 YAML，适合自定义 config）
├── job_config.yaml.template    # ComposeJobConfig YAML 模板（含占位符）
├── main.sh                     # 主容器入口脚本（harbor runner，原 Agent-Hub/task/harbor/main.sh + 两层适配）
└── sidecars/
    └── proxy-sidecar.sh        # cc-proxy sidecar 脚本（原 Agent-Hub/task/harbor/proxy-sidecar.sh）
```

## 快速开始（推荐：harbor_compose_demo.py）

这是经过 ROCK 真机端到端验证的脚本，对应你的 AP harbor 命令，所有 dockerd/网络/挂载
修正都已内置，只需配凭证：

```bash
cd examples/job/compose
cp .env.example .env          # 填入 ROCK_TOKEN / MODEL_* / OSS_* 凭证
source .env
uv run python harbor_compose_demo.py
```

**OSS 凭证是必需的**：harbor 从 OSS 下载 dataset（`terminal-bench/aone-bench-java100`）。
AP 平台自动注入 OSS 凭证，SDK 直连模式需你在 `.env` 里显式提供
（OSS_BUCKET / OSS_ENDPOINT / OSS_REGION / OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET）。

任务参数（INSTANCE_ID / DATASET / HARBOR_AGENT 等）都有默认值（对应 AP `-p`），
可用环境变量覆盖，详见 `harbor_compose_demo.py` 顶部 docstring。

## 运行方案：runner.sh 在外层沙箱主动启动 dockerd

> 以下要点均经过 ROCK 真实后端（`xrl.alibaba-inc.com` / `vpc-sg-a`，kata runtime）端到端验证。

外层沙箱镜像**必须自带 `docker` / `dockerd` / `containerd` / `runc`**（例如 harbor runner 镜像）。

**重要**：不要用 `docker:27-dind` 作外层镜像 —— 实测该镜像变体在 ROCK kata 沙箱内
缺少 `containerd`，dockerd 无法启动。请用一个预装完整 docker 工具链的业务镜像。

**dockerd 不会自动启动**：ROCK kata 沙箱进入时没有运行 dockerd。`ComposeTrial`
生成的 `runner.sh` 会在 P0 阶段主动启动它，并内置两个 kata 环境必需的修正：

```bash
PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin \
DOCKER_IGNORE_BR_NETFILTER_ERROR=1 \
    nohup dockerd >/var/log/dockerd.log 2>&1 &
```

- **显式 PATH**：`nohup` 启动的 dockerd 不继承交互 shell 的 PATH，否则报
  `containerd executable file not found`。
- **`DOCKER_IGNORE_BR_NETFILTER_ERROR=1`**：kata guest 缺
  `/proc/sys/net/bridge/bridge-nf-call-iptables`，否则 bridge 网络初始化失败。

这些已固化在 `rock/sdk/job/compose/trial.py` 的 runner 模板里，用户无需关心。

### 三层 DinD 结构（注意事项）

```
ROCK 外层 kata 沙箱（业务镜像，自带 docker 工具链）
└── runner.sh（ComposeTrial 生成，P0 启动 dockerd，再用 docker CLI 编排）
    ├── proxy sidecar 容器（cc-proxy，监听 8082，--network-alias proxy）
    └── main 容器（harbor runner，privileged=true，挂载 /rock/scripts）
        └── harbor CLI → 在内层再起 task env 容器（第三层）
```

**关键约束**：
- 外层沙箱须开启 `use_kata_runtime: true`
- main 容器须 `privileged: true`（harbor 内部起 docker 容器需要）
- runner.sh 自动把外层 `/rock/scripts` 以 `-v /rock/scripts:/rock/scripts:ro`
  挂载进 main / init / sidecar 容器，使各容器能执行上传的脚本
- 主容器入口固定为 `bash /rock/scripts/main.sh`，因此**业务镜像须自带 `bash`**
- proxy 访问：sidecar 以 `--network-alias proxy` 注册，同一 compose network 内
  main 容器可用 `http://proxy:8082` 直达。注意：原始 Agent-Hub main.sh 用
  `docker network inspect bridge` 取 gateway IP 访问 proxy，这是 K8s 同 Pod 模型；
  在 compose 独立容器模型下若 proxy 不在 main 的 bridge 网络，需改用 `proxy:8082`
  alias（见 main.sh 顶部注释）

## 环境变量

运行前需导出以下环境变量（或写入 `.env` 后 `source .env`）：

```bash
# 模型
export MODEL=claude-opus-4-8
export MODEL_API_KEY=<your-api-key>
export MODEL_BASE_URL=<your-base-url>

# ROCK 集群
export ROCK_TOKEN=<your-rock-token>

# OSS 凭证
export OSS_ACCESS_KEY_ID=<ak>
export OSS_ACCESS_KEY_SECRET=<sk>
export OSS_REGION=cn-hangzhou
export OSS_ENDPOINT=oss-cn-hangzhou-internal.aliyuncs.com
export OSS_BUCKET=<bucket>
```

## 使用步骤

### 1. 准备配置文件

```bash
cp examples/job/compose/job_config.yaml.template examples/job/compose/job_config.yaml
# 编辑 job_config.yaml，填入真实镜像名和占位符
```

主要需要替换的占位符：

| 占位符 | 说明 |
|--------|------|
| `<ROCK_TOKEN>` | ROCK 集群认证 token |
| `<MODEL>` | 模型名，e.g. `claude-opus-4-8` |
| `<MODEL_API_KEY>` | 模型 API Key |
| `<MODEL_BASE_URL>` | 模型 Base URL |
| `<INSTANCE_ID>` | 任务 ID，e.g. `mailman` |
| `<HARBOR_MAIN_IMAGE>` | harbor runner 镜像（含 harbor CLI + claude-code） |
| `<CC_PROXY_IMAGE>` | claude-code proxy 镜像 |
| `<OSS_*>` | OSS 凭证和 bucket 信息 |

### 2. 运行

```bash
python examples/job/compose/compose_demo.py -c examples/job/compose/job_config.yaml
```

### 3. 查看结果

脚本会打印 `exit_code`、`score` 和各 trial 结果。
产物（harbor_stdout.txt、result.json、metrics.json 等）通过 `oss_mirror` 上传到 OSS。

## proxy sidecar 端口

`proxy-sidecar.sh`（原 Agent-Hub/task/harbor/proxy-sidecar.sh）监听端口 **8082**。
配置中 `sidecars[].health.port = 8082`，runner.sh 会在主容器启动前探测该端口就绪。

## 与 HarborJobConfig 的对比

本示例展示 ComposeJobConfig 的表达能力。实际上，对于"调 harbor CLI 跑 benchmark"的场景，
`HarborJobConfig` 更原生（自带 agents/datasets/verifier 结构化支持）。

ComposeJobConfig 更适合：自己掌控每个容器镜像和脚本、容器间是简单"主 + sidecar + init"拓扑。
