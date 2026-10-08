#!/usr/bin/env bash
set -euo pipefail
ROCK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${TASK:?Set TASK}"
: "${ROLL:?Set ROLL}"
mode="${1:-rollout}"; (($# == 0)) || shift
case "$mode" in
 rollout) entry=eval_swe_bench.py ;;
 ppo) entry=train_swe_bench.py ;;
 ppo-kl) entry=train_swe_bench_kl.py ;;
 grpo) entry=train_swe_bench_grpo.py ;;
 *) echo 'Usage: run.sh rollout|ppo|ppo-kl|grpo [cookbook arguments]' >&2; exit 2 ;;
esac
PYTHON="$TASK/control-venv/bin/python"
export PYTHONPATH="$ROCK/rock-tinker/src:$ROCK/rock-tinker:$ROCK/tinker-backend/src:$ROCK:$ROLL"
export ROCK_CONFIG="$TASK/config/admin.yaml"
export PYTHON_DOTENV_DISABLED=1 TINKER_LOG=info
export TINKER_JOBS_ROOT="$TASK/jobs"
umask 077
mkdir -p "$TASK/runs"
RUN_DIR="$(mktemp -d "$TASK/runs/$mode-$(date +%Y%m%dT%H%M%S)-XXXX")"
export DATABASE_URL="sqlite+aiosqlite:///$RUN_DIR/backend.sqlite3"
cd "$RUN_DIR"
printf 'Run directory: %s\n' "$RUN_DIR"
task_id="${TASK_ID:-$("$PYTHON" -c 'import json,os; print(json.load(open(os.environ["TASK"]+"/config/manifest.json")).get("task_id", "sympy__sympy-19637"))')}"
max_tokens=8192
set +e
"$PYTHON" "$ROCK/rock-tinker/tinker_cookbook/tinker_backend_cookbook/$entry" \
 --config-path "$TASK/config/runtime.yaml" --output-path "$RUN_DIR/results" \
 --task-id "$task_id" --max-turns 32 --max-tokens "$max_tokens" --temperature 1.0 "$@" > "$RUN_DIR/client.log" 2>&1
status=$?
printf '{"exit_code": %s}\n' "$status" > "$RUN_DIR/exit-status.json"
printf 'Exit code: %s; log: %s/client.log\n' "$status" "$RUN_DIR"
exit "$status"
