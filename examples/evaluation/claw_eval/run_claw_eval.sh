#!/bin/bash
# Claw-eval BashJob infra script.
#
# Environment variables (passed via `rock job run --env`):
#   RUN_CMD      — claw-eval command to execute (required)
#                  e.g. "claw-eval batch --parallel 4 --sandbox --config /tmp/claw-eval-config/config.yaml"
#   AGENT_IMAGE  — Docker image to pull before running (optional)
#   WORK_DIR     — working directory before eval RUN_CMD (optional, default /workspace)
#   SERP_DEV_KEY — API key forwarded to claw-eval (optional)
#
# Usage:
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

ARTIFACT_DIR="${ROCK_BASH_JOB_ARTIFACT_DIR:-/data/logs/user-defined}"
LOG_DIR="$ARTIFACT_DIR/logs"

# ── 1. Prepare log directory ───────────────────────────────
mkdir -p "$LOG_DIR"

# ── 2. Start dockerd (DinD) ───────────────────────────────
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

# ── 3. Pull agent image (optional) ────────────────────────
[ -n "$AGENT_IMAGE" ] && docker pull "$AGENT_IMAGE"

# ── 4. Execute RUN_CMD ────────────────────────────────────
[ -z "$RUN_CMD" ] && { echo "ERROR: RUN_CMD environment variable is not set"; exit 1; }
cd "${WORK_DIR:-/workspace}"
eval "$RUN_CMD" 2>&1 | tee "$LOG_DIR/run.log"

# ── 5. Score summary (parse run.log) ──────────────────────
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

# ── 6. Write score.json ───────────────────────────────────
cat > "$ARTIFACT_DIR/score.json" <<EOF
{
  "task_score": ${TASK_SCORE:-null},
  "completion": ${COMPLETION:-null},
  "robustness": ${ROBUSTNESS:-null},
  "communication": ${COMMUNICATION:-null},
  "safety": ${SAFETY:-null},
  "passed": ${PASSED:-null},
  "wall_time": ${WALL_TIME:-null},
  "tokens": ${TOKENS:-null}
}
EOF
echo "Score written to $ARTIFACT_DIR/score.json"
