#!/bin/bash
# Claw-eval BashJob infra 脚本
#
# 通过 `rock job run --env` 传入的环境变量：
#   RUN_CMD      — 要执行的 claw-eval 命令（必填）
#                  例: "claw-eval batch --parallel 4 --sandbox --config /tmp/claw-eval-config/config.yaml"
#   AGENT_IMAGE  — 运行前需要 pull 的 Docker 镜像（可选）
#   WORK_DIR     — 执行 RUN_CMD 前 cd 的目录（可选，默认 /workspace）
#   SERP_DEV_KEY — 透传给 claw-eval 的 API key（可选）
#
# 用法：
#   rock job run --type bash \
#     --script examples/agents/claw_eval/run_claw_eval.sh \
#     --image "<YOUR_IMAGE>" \
#     --base-url "<YOUR_BASE_URL>" \
#     --cluster "<YOUR_CLUSTER>" \
#     --memory 64g --cpus 16 --timeout 7200 \
#     --env "SERP_DEV_KEY=<YOUR_SERP_KEY>" \
#     --env "AGENT_IMAGE=<YOUR_SANDBOX_AGENT_IMAGE>" \
#     --env "RUN_CMD=claw-eval batch --parallel 4 --sandbox --config /tmp/claw-eval-config/config.yaml --trace-dir /data/logs/user-defined/traces" \
#     --local-path . --target-path /tmp/claw-eval-config

set -eo pipefail

LOG_DIR="/data/logs/user-defined"

# ── 1. 准备日志目录 ────────────────────────────────────────
mkdir -p "$LOG_DIR"

# ── 2. 启动 dockerd (DinD) ────────────────────────────────
if command -v docker &>/dev/null; then
    if ! pgrep -x dockerd &>/dev/null; then
        echo "Starting dockerd..."
        nohup dockerd &>/var/log/dockerd.log &
    fi
    for i in $(seq 1 60); do
        docker info &>/dev/null && { echo "dockerd ready"; break; }
        sleep 1
        [ "$i" -eq 60 ] && echo "WARN: dockerd failed to start within 60s"
    done
fi

# ── 3. Pull agent 镜像（可选）──────────────────────────────
[ -n "$AGENT_IMAGE" ] && docker pull "$AGENT_IMAGE"

# ── 4. 执行 RUN_CMD ───────────────────────────────────────
[ -z "$RUN_CMD" ] && { echo "ERROR: RUN_CMD environment variable is not set"; exit 1; }
cd "${WORK_DIR:-/workspace}"
eval "$RUN_CMD" 2>&1 | tee "$LOG_DIR/run.log"

# ── 5. Score 汇总（解析 run.log）──────────────────────────
echo "=== Score Summary ==="
LOG_FILE="$LOG_DIR/run.log"
TEXT=$(cat "$LOG_FILE")
get_float() { echo "$TEXT" | grep -oP "$1:\s+\K[\d.]+" | tail -1; }
TASK_SCORE=$(get_float "task_score")
COMPLETION=$(get_float "completion")
ROBUSTNESS=$(get_float "robustness")
COMMUNICATION=$(get_float "communication")
SAFETY=$(get_float "safety")
PASSED=$(echo "$TEXT" | grep -oP 'passed:\s+\K(True|False)' | tail -1)
WALL_TIME=$(echo "$TEXT" | grep -oP 'wall=\K[\d.]+' | tail -1)
TOKENS=$(echo "$TEXT" | grep -oP 'tokens=\K\d+' | tail -1)
echo "task_score=${TASK_SCORE:-N/A} completion=${COMPLETION:-N/A} robustness=${ROBUSTNESS:-N/A}"
echo "communication=${COMMUNICATION:-N/A} safety=${SAFETY:-N/A} passed=${PASSED:-N/A}"
echo "wall_time=${WALL_TIME:-N/A}s tokens=${TOKENS:-N/A}"
