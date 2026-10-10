---
sidebar_position: 8
title: Tinker 快速开始
---

# Tinker 快速开始

Tinker 用于通过统一的 Python SDK 执行 agent rollout 和强化学习训练。本仓库提供 Tinker SDK、local backend 和 cookbook，支持 PPO、PPO+KL、GRPO：客户端提交采样与训练请求，backend 管理本地任务生命周期，ROLL 在 GPU 上执行模型推理和训练，ROCK 管理 Harbor 沙箱中的 SWE-agent 工具执行。SWE-agent 通过 ROCK ModelService 回调本地模型服务，评测结果由 SWE-bench verifier 给出。

Tinker 控制环境使用独立的依赖声明和锁文件；SDK、backend 与 cookbook 随统一 rl-rock wheel 打包。下面的步骤从环境准备开始，跑通公开 SWE-bench Verified 单题 rollout，并介绍如何切换到训练。

这套接入支持公开 Harbor 格式的所有 SWE-bench Verified 题目：完整沿用每道题的环境构建和原始 verifier，统一接入 SWE-agent 与 ModelService。本文使用 Qwen3 4B，仅选一道题演示安装、rollout 和训练流程。

执行链路：Tinker client → local backend → ROLL 模型服务 → ROCK ModelService → Harbor 中的 SWE-agent → SWE verifier。模型在 GPU 上推理，工具在沙箱内执行。

## 1. 环境与代码

使用 Linux x86_64、Python 3.12，准备可用的 Docker、GPU 和公开网络访问。Docker 需要允许启动 privileged 容器。示例按 8 张 GPU 配置：rollout 使用 GPU 4–7，训练使用全部 8 张；设备数量或分配不同时先修改生成的 YAML。本文使用以下 GPU 基础镜像，已包含 Torch 2.11、vLLM 0.23 和 CUDA 13：

```text
roll-registry.cn-hangzhou.cr.aliyuncs.com/roll/pytorch:nvcr-26.03-py3-torch2110-vllm0230
```

```bash
nvidia-smi
docker info
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

git clone https://github.com/alibaba/ROCK.git
git clone --branch feat/tinker-runtime --single-branch https://github.com/alibaba/ROLL.git
export ROCK="$(realpath ROCK)"
export ROLL="$(realpath ROLL)"
export TASK="$HOME/tinker-local"
export QS="$ROCK/examples/tinker_quick_start"
mkdir -p "$TASK/logs"
```

确认 `$QS/setup.sh` 和 `$ROLL/requirements_tinker.txt` 存在。每个新终端设置相同的 `ROCK`、`ROLL`、`TASK`、`QS`，完成第 4 节后还需设置相同的 `TASK_ID`，并执行 `export PY="$TASK/control-venv/bin/python"`。`TASK` 使用新的绝对路径。

## 2. 安装 ROLL 环境

Tinker 增量依赖在 ROLL 的 `requirements_tinker.txt`，对照官方 `requirements_common.txt` 和 `requirements_torch2100_vllm.txt` 维护。指定镜像已提供 GPU 软件栈，安装脚本通过 uv 在继承镜像包的独立环境中补充依赖，并核对 Torch、vLLM、CUDA、Transformer Engine 等包的版本与来源路径没有变化。

```bash
bash "$QS/setup.sh" runtime --gpu-check > "$TASK/logs/setup-runtime.log" 2>&1
cat "$TASK/runtime-venv/setup-public-runtime.json"
```

`--gpu-check` 使用可见 GPU 0 执行小型检查。机器有其他任务时，可在命令前设置 `CUDA_VISIBLE_DEVICES=<空闲GPU编号>`。

## 3. 安装 ROCK 和 Tinker

```bash
bash "$QS/setup.sh" control > "$TASK/logs/setup-control.log" 2>&1
export PY="$TASK/control-venv/bin/python"
cat "$TASK/control-venv/import-check.json"
```

该步骤执行 `uv sync --project "$ROCK/rock-tinker/local" --frozen --group control --no-dev`，使用 `rock-tinker/local/pyproject.toml` 和对应 `uv.lock` 安装独立的 Tinker 控制环境，再构建统一 `rl-rock` wheel。ROCK 根目录的默认依赖、Python 范围和包源保持原样。控制环境使用 CPU PyTorch；GPU 环境复用镜像的软件栈，两者不混用。wheel 位于 `$TASK/dist/`。

运行时模板统一放在 `rock-tinker/tinker_cookbook/config/`，并包含在 wheel 中。下文会将完整的公开 Harbor 任务下载到 `$TASK/assets/harbor-tasks/`，在 `$TASK/config/` 下生成本次运行的 YAML，无需修改源码中的模板。

## 4. 准备公开数据和 Harbor 沙箱

```bash
export TASK_ID=sympy__sympy-19637
WHEELS=("$TASK"/dist/rl_rock-*.whl)
test "${#WHEELS[@]}" -eq 1 && test -f "${WHEELS[0]}" || exit 1
"$PY" "$QS/prepare_sandbox.py" --rock-root "$ROCK" --task "$TASK" --task-id "$TASK_ID" \
  --wheel "${WHEELS[0]}" \
  > "$TASK/logs/prepare-sandbox.log" 2>&1
```

这一步仅下载选中的公开 Harbor 题目，准备 Harbor 控制镜像 `tinker-harbor:local`，其中包含可复用的固定版本 SWE-agent 运行环境。题目的 `instruction.md`、`task.toml`、完整 `environment/Dockerfile` 和原始 verifier 保持不变。Harbor 按题目原有定义构建和执行环境；SWE-agent 使用独立的 Python 环境，其依赖不会替换题目自身的依赖。运行环境自动准备，无需手工进入题目容器。

题目位于 `$TASK/assets/harbor-tasks/$TASK_ID/`，来源和校验信息保存在 `$TASK/sandbox-preparation/manifest.json`。镜像和 agent 运行环境准备后可复用。切换到其他题目（例如 `pallets__flask-5014`）时，修改 `TASK_ID`，并在准备、配置和执行三个步骤中使用相同 ID；使用新的 `TASK` 目录可隔离不同示例的配置与结果。沙箱网络需要能够访问该题目的镜像和 verifier 依赖。

## 5. 配置本地运行

```bash
export NODE_IP="$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([^ ]*\).*/\1/p' | head -1)"
"$PY" "$QS/configure.py" \
  --roll-root "$ROLL" --task-root "$TASK" --task-id "$TASK_ID" \
  --model Qwen/Qwen3-4B-Instruct-2507 \
  --outer-image tinker-harbor:local --node-ip "$NODE_IP" --rollout-only
```

ROLL 根据模型 ID 自动下载并缓存权重，也支持 `--model /绝对路径/模型目录`。默认来源为 Hugging Face；使用 ModelScope 时增加 `--model-download-type MODELSCOPE`。

生成后只需按机器情况检查以下参数：

| 文件 | 常用配置 |
|---|---|
| `config/engine.yaml` | `pretrain`、`actor_infer.device_mapping`、推理并行度和长度预算 |
| `config/harbor.yaml` | ROCK 地址、`cluster: local`、沙箱资源、题目路径 |
| `config/runtime.yaml` | backend 地址、GPU Python 路径、检查点目录 |
| `config/admin.yaml` | 本地 ROCK 服务配置 |

默认 Admin 端口为 18080，backend 为 19210，宿主 ROCKlet 为 22555，ModelService 为 28080；推理使用 GPU 4–7。修改 GPU 分配时保持并行度与设备数一致。`--rollout-only` 关闭训练和参考模型，不占用训练 GPU。生成器拒绝覆盖已有配置，后续可以直接编辑 YAML。

## 6. 启动本地 ROCK

先确认 18080、19210、22555、28080 没有被其他任务占用。两个终端分别执行，进程保持前台运行：

```bash
# 终端 A
bash "$QS/serve.sh" worker
```

```bash
# 终端 B
bash "$QS/serve.sh" admin
```

另一个终端等待服务就绪：

```bash
curl -fsS http://127.0.0.1:18080/
ss -lntp | grep -E ':18080|:22555'
```

本地 ROCK 会启动所需 Ray 组件，无需另外启动一套 Ray head。只有服务使用同一份 `TASK/config/admin.yaml` 和密钥时才可复用。切换 TASK 时，先用 Ctrl-C 停止自己启动的两个服务，再按本节用新 TASK 启动；不要停止其他用户的服务。

本地更换 Admin 端口时，在第 5 节的配置命令中增加 `--admin-port 18081`；生成的 ROCK base URL 和 `serve.sh admin` 监听端口会同步变为 `http://127.0.0.1:18081`，上面的健康检查地址也相应修改。

### 连接自己搭建的 ROCK 集群

默认 ROCK 地址为 `http://127.0.0.1:18080`，集群名为 `local`。已有 ROCK 服务时，在生成新配置时指定其 Admin API 地址和实际集群名：

```bash
export ROCK_BASE_URL=https://rock.example.com
export ROCK_CLUSTER=my-cluster
"$PY" "$QS/configure.py" \
  --roll-root "$ROLL" --task-root "$TASK" --task-id "$TASK_ID" \
  --model Qwen/Qwen3-4B-Instruct-2507 \
  --outer-image registry.example.com/team/tinker-harbor:local \
  --node-ip "$NODE_IP" --rollout-only \
  --rock-base-url "$ROCK_BASE_URL" --rock-cluster "$ROCK_CLUSTER"
```

用这条命令替代第 5 节的配置命令。将准备好的 Harbor 控制镜像发布到集群可访问的镜像仓库，并填入真实镜像名。连接已有服务时，跳过上面的本地 worker/admin 启动步骤；Tinker 和 ROLL 仍在本地运行。

如果已经生成配置，修改 `$TASK/config/harbor.yaml` 中的这些字段即可：

```yaml
environment:
  base_url: https://rock.example.com
  cluster: my-cluster
  image: registry.example.com/team/tinker-harbor:local
  extra_headers: {}
```

保留其他字段。集群需要认证时，将管理员提供的请求头名称和值填写到 `extra_headers`，该配置会传给 ROCK SDK。无需把本地 Admin 的加密密钥复制到远端集群，也不要提交凭据。Tinker 所在机器需要能够访问 ROCK SDK 使用的 Admin API 和沙箱端点。ModelService 运行在 ROCK 沙箱内：内层 SWE-agent 访问该服务（默认端口 28080），pull runner 通过 ROCK 命令通道交换推理请求和响应，因此无需沙箱通过 HTTP 直接连接 GPU backend。集群需要允许内层 Harbor 题目容器访问外层沙箱中的 ModelService。

## 7. 启动 Tinker 并运行 rollout

```bash
bash "$QS/run.sh" rollout --task-id "$TASK_ID"
```

该命令调用 cookbook `eval_swe_bench.py`，自动启动 local backend、等待 GPU runtime、创建 client 并执行任务，结束后关闭本次 backend/runtime。无需另起一个占用相同端口的 backend。

默认使用同一道题，最多 32 轮、每次最多生成 8192 tokens，temperature 为 1.0。配置生成器为独立 rollout、PPO、PPO+KL 和 GRPO 统一采用最后验证得分的 rollout 预算，无需再执行配置补丁：

| 配置 | 默认值 |
|---|---:|
| agent 输入 tokens | 32768 |
| agent 输出 tokens | 8192 |
| 推理上下文长度 | 65536 |
| 单次工具观察字符数 | 12000 |
| 保留最近工具观察数 | 8 |

后续可直接编辑 `config/harbor.yaml` 和 `config/engine.yaml`。输入加输出不能超过模型服务上下文上限；修改输出预算时同时调整 agent 的 `max_output_tokens`、`completion_kwargs.max_tokens` 和 client 的 `--max-tokens`。保存后重新运行 rollout。增加预算或沿用同一配置都不保证每次解题成功，结果以 verifier reward 为准。

## 8. 查看 cookbook 客户端的结果和日志

rollout、PPO、PPO+KL 和 GRPO 都由 `rock-tinker` 中的 cookbook 发起，通过 Tinker SDK 与 backend 交互。用户首先查看 cookbook 客户端的输出和保存的结果，无需另外编写 SDK 查询程序。

`run.sh` 会在终端打印本次 `Run directory`，将 cookbook 的输出写入该目录的 `client.log`，结束时打印退出码和日志路径。在另一个终端设置相同的 `TASK` 和 `PY` 后查看：

```bash
MODE=rollout  # 可改为 ppo、ppo-kl 或 grpo
RUN_DIR="$(ls -td "$TASK/runs/$MODE-"* | head -1)"
tail -f "$RUN_DIR/client.log"
```

用 Ctrl-C 退出日志查看不会停止另一个终端的任务。任务结束后检查客户端退出状态，并读取结果文件：

```bash
cat "$RUN_DIR/exit-status.json"
RESULT_DIR="$(find "$RUN_DIR/results" -mindepth 1 -maxdepth 1 -type d | head -1)"
if [ "$MODE" = rollout ]; then
  "$PY" -m json.tool "$RESULT_DIR/summary.json"
else
  "$PY" -m json.tool "$RESULT_DIR/result.json"
fi
```

也可以使用 `client.log` 中 `Results dir:` 打印的精确结果目录。结果文件在相应阶段完成后生成；若任务提前失败，先查看 `client.log` 中的错误。

| 模式 | cookbook 保存的结果（相对于 `RESULT_DIR`） | 主要查看内容 |
|---|---|---|
| rollout | `summary.json` | `task_id`、`reward`、`episode_done`、`turns_used`、`time_seconds` |
| PPO / PPO+KL | `result.json`、`trajectory.txt` | rollout 的 `reward`，训练的 `fwdbwd_metrics`、`optim_metrics`、`train_elapsed`，以及 `published_sampling_client` |
| GRPO | `result.json`、`step_000/step_result.json` 等 | 运行汇总 `steps`；每一步的 `group_returns`、`group_advantages`、`fwdbwd_metrics` 和 `optim_metrics` |
| GRPO 每条轨迹 | `step_000/rollout_00.txt`、`step_000/rollout_00.json` 等 | 模型/工具交互、该轨迹的 `return`、`advantage` 和 `turns` |
| 保存训练状态 | `checkpoint.json`（仅传入 `--save-state NAME` 时） | backend 返回的 checkpoint `name` 和 `path`；此 JSON 是位置记录，不是模型权重文件 |

完整 rollout 的成功标志是 `exit-status.json` 中 `exit_code=0`，并且生成的 `summary.json` 中 `episode_done=true`。随后查看 `reward`：`reward=1` 表示解题成功，正常评分后的 `reward=0` 表示本次未解决题目。如果客户端报错、被中断或没有生成摘要，即使沙箱中已有评分文件，也不能视为客户端全流程成功。`SESSION_END` 表示 agent 进程结束，本身不是超时错误。独立 rollout 的 cookbook 当前保存摘要和首个动作的 token，不保存完整 agent 轨迹。

训练时，`client.log` 会依次输出 `Rollout done`（GRPO 为组内 rollout 进度）、`forward_backward done`、`optim_step done` 和 `Weights published to sampler`。结合结果中的训练指标确认完成了训练与权重发布；如果出现 `skipping step`、`skipping train step` 或 `nothing to train on`，说明对应训练步骤被跳过，不能只凭退出码为 0 判断训练已完成。GRPO 的 `rollouts_succeeded` 表示轨迹成功执行，不等于得分为 1 的条数；得分查看 `group_returns`。客户端的结果文件在 backend/runtime 关闭后仍保留在本地。

需要排查客户端报错时，再查看底层日志：

| 组件 | 日志位置 |
|---|---|
| Tinker backend 与 ROLL GPU runtime | `$TASK/jobs/<任务>/`；`client.log` 的 `TINKER_SERVER_JOB` 行给出本次 `log_dir` |
| 本地 ROCK Admin / ROCKlet | `$TASK/logs/rock-admin.log`、`$TASK/logs/rock-worker.log` |
| Harbor、SWE-agent 与 verifier | 运行期间沙箱内 `/data/logs/user-defined/jobs/` |

Harbor trial 目录中的原始评分文件为 `verifier/report.json` 和 `verifier/reward.txt`。依赖下载或执行失败需要结合日志排查，不能只凭 reward 文件判断题目是否完成了有效评分。沙箱结束后会删除，需要完整 agent/verifier 轨迹时应在运行期间导出；日常查看结果以 cookbook 的本地输出为入口。

## 9. 按需运行训练

训练使用 8 张空闲 GPU。PPO、PPO+KL 和 GRPO 共用第 7 节的 rollout 预算：输入 32768 tokens、输出 8192 tokens、上下文 65536 tokens，保留最近 8 条、每条最多 12000 字符的工具观察。生成器会同时启用训练、参考模型和推理。预算增大也会增加训练显存需求。已经生成的配置不会自动更新，请按下方步骤创建新的训练目录。

从 rollout 切换到训练时，先等 client 退出，用 Ctrl-C 停止自己启动的本地 worker/admin，共享 ROCK 集群保持运行；保留原 TASK，在同一终端创建新的训练目录：

```bash
export ROLLOUT_TASK="$TASK"
export TASK="$HOME/tinker-train"
mkdir -p "$TASK/logs"
ln -s "$ROLLOUT_TASK/runtime-venv" "$TASK/runtime-venv"
ln -s "$ROLLOUT_TASK/control-venv" "$TASK/control-venv"
ln -s "$ROLLOUT_TASK/assets" "$TASK/assets"
export PY="$TASK/control-venv/bin/python"
"$PY" "$QS/configure.py" \
  --roll-root "$ROLL" --task-root "$TASK" --task-id "$TASK_ID" \
  --model Qwen/Qwen3-4B-Instruct-2507 \
  --outer-image tinker-harbor:local --node-ip "$NODE_IP"
```

复用已安装环境、公开题目和沙箱镜像，不重新下载。此目录必须未配置过。连接已有 ROCK 集群时，配置命令还需沿用第 6 节的 `--rock-base-url`、`--rock-cluster` 和镜像仓库地址；使用本地 ROCK 时，为其按第 6 节重新启动 worker/admin。然后运行下面的命令。

```bash
bash "$QS/run.sh" ppo --learning-rate 1e-6 --lora-rank 0 --max-train-transitions 1
bash "$QS/run.sh" ppo-kl --learning-rate 1e-6 --lora-rank 0 --kl-beta 0.05
bash "$QS/run.sh" grpo --learning-rate 1e-6 --lora-rank 0 \
  --num-steps 1 --group-size 2 --rollout-concurrency 1
```

三种命令依次执行，等待上一种退出后再启动下一种。PPO 示例只用一条 transition 检查训练调用；去掉 `--max-train-transitions 1` 才训练全部轨迹。需要保存状态时追加 `--save-state quick-start-final`。训练命令完成不等于模型效果提升，效果需另做前后评测。
