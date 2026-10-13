#!/usr/bin/env bash
set -euo pipefail
: "${TASK:?Set TASK}"
PYTHON="$TASK/control-venv/bin/python"
export ROCK_CONFIG="$TASK/config/admin.yaml" PYTHON_DOTENV_DISABLED=1
mkdir -p "$TASK/logs"
umask 077
case "${1:-}" in
 admin)
  export ROCK_ADMIN_ENV=local ROCK_ADMIN_ROLE=admin ROCK_DEFAULT_CLUSTER=local
  port="$("$PYTHON" -c 'import json,os; print(json.load(open(os.environ["TASK"]+"/config/manifest.json"))["admin_port"])')"
  exec "$PYTHON" -m uvicorn rock.admin.main:create_app --factory --host 127.0.0.1 --port "$port" --workers 1 > "$TASK/logs/rock-admin.log" 2>&1 ;;
 worker)
  unset ROCK_ADMIN_ROLE ROCK_ADMIN_ENV
  node="$("$PYTHON" -c 'import json,os; print(json.load(open(os.environ["TASK"]+"/config/manifest.json"))["node_ip"])')"
  exec "$TASK/control-venv/bin/rocklet" --host "$node" --port 22555 > "$TASK/logs/rock-worker.log" 2>&1 ;;
 *) echo 'Usage: serve.sh admin|worker (foreground)' >&2; exit 2 ;;
esac
