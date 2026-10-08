#!/usr/bin/env bash
# Install the locked local control environment and build the unified wheel.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROCK_ROOT="${ROCK_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
ROLL_ROOT="${ROLL_ROOT:-$(dirname "$ROCK_ROOT")/ROLL}"
CONTROL_VENV="${CONTROL_VENV:-$(dirname "$ROCK_ROOT")/tinker-local/control-venv}"
CONTROL_DIST_DIR="${CONTROL_DIST_DIR:-$(dirname "$CONTROL_VENV")/dist}"
BASE_PYTHON="${BASE_PYTHON:-python3}"
if [[ "${1:-}" == --help ]]; then
 echo 'Usage: ROCK_ROOT=... ROLL_ROOT=... CONTROL_VENV=... bash setup_control.sh'
 echo 'Runs uv sync --frozen --group control in an isolated environment.'
 exit 0
fi
command -v uv >/dev/null
[[ -f "$ROCK_ROOT/rock-tinker/local/uv.lock" && -d "$ROLL_ROOT/roll" ]]
if [[ -e "$CONTROL_VENV" && ! -f "$CONTROL_VENV/.tinker-locked-control-owner" ]]; then
 echo 'Use a fresh CONTROL_VENV or the existing environment created by this script.' >&2
 exit 2
fi
export UV_PROJECT_ENVIRONMENT="$CONTROL_VENV"
uv sync --project "$ROCK_ROOT/rock-tinker/local" --python "$BASE_PYTHON" --frozen --group control --no-dev
printf '%s\n' "$ROCK_ROOT" > "$CONTROL_VENV/.tinker-locked-control-owner"
mkdir -p "$CONTROL_DIST_DIR"
uv build --project "$ROCK_ROOT" --wheel --out-dir "$CONTROL_DIST_DIR"
export PYTHONPATH="$ROCK_ROOT:$ROLL_ROOT" PYTHON_DOTENV_DISABLED=1
PYTHON="$CONTROL_VENV/bin/python"
TORCH_VERSION="$("$PYTHON" -c 'import importlib.metadata; print(importlib.metadata.version("torch"))')"
"$PYTHON" "$SCRIPT_DIR/setup_control_requirements.py" verify --output "$CONTROL_VENV/import-check.json" --torch-version "$TORCH_VERSION"
echo 'Locked control environment verified. No services or GPU runtime started.'
