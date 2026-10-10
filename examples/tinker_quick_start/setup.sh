#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${TASK:?Set TASK to a new working directory}"
: "${ROLL:?Set ROLL to the ROLL checkout}"
mkdir -p "$TASK/logs"
case "${1:-}" in
  runtime) shift; bash "$ROLL/examples/tinker_backend_runtime/setup_public_runtime.sh" --roll-root "$ROLL" --venv "$TASK/runtime-venv" "$@" ;;
  control) ROCK_ROOT="$ROOT" ROLL_ROOT="$ROLL" CONTROL_VENV="$TASK/control-venv" CONTROL_DIST_DIR="$TASK/dist" bash "$ROOT/examples/tinker_quick_start/setup_control.sh" ;;
  *) echo 'Usage: setup.sh runtime [--gpu-check] | control' >&2; exit 2 ;;
esac
