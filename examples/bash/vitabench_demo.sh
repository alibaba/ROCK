#!/bin/bash
# Simple bash job demo using `rock job run`
#
# This script defines a bash job inline and executes it inside a ROCK sandbox
# via the `rock job run` CLI command.

set -euo pipefail

# ===== Configuration =====
# Override via environment variables: YOUR_API_KEY, YOUR_USER_ID, YOUR_EXPERIMENT_ID
ROCK_BASE_URL="${ROCK_BASE_URL}"
YOUR_API_KEY="${YOUR_API_KEY}"
YOUR_USER_ID="${YOUR_USER_ID}"
YOUR_EXPERIMENT_ID="${YOUR_EXPERIMENT_ID}"
ROCK_IMAGE="${ROCK_IMAGE:-rl-rock-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11}"
ROCK_CLUSTER="${ROCK_CLUSTER:-vpc-sg-sl-a}"
LOCAL_WORKSPACE_DIR="${LOCAL_WORKSPACE_DIR}"

EXTERNAL_VARIABLE_1="external_value"
TO_RENDERED_KEYS=(
    "EXTERNAL_VARIABLE_1"
    "ROCK_WORKSPACE_DIR"
)
ROCK_WORKSPACE_DIR="/root/workspace/vitabench"
# =========================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Define the bash job script content
# This is the script that will be uploaded and executed inside the sandbox
read -r -d '' BASH_SCRIPT << 'EOF' || true
#!/bin/bash
set -euo pipefail

cd ${ROCK_WORKSPACE_DIR}
apt update && apt install -y curl
curl -LsSf https://astral.sh/uv/install.sh | sh
source /root/.local/bin/env
rm -rf .venv
uv venv .venv
source .venv/bin/activate
uv pip install -e . -i https://mirrors.aliyun.com/pypi/simple
mkdir -p /data/logs/user-defined

echo "y" | vita run --domain delivery --user-llm gpt-4.1-2025-04-14 --agent-llm qwen3.6-plus-preview --evaluator-llm claude-sonnet-4-20250514 --num-trials 2 --max-steps 300 --max-concurrency 32 --language chinese --log-level WARNING --seed 1
echo ""
echo "Job completed successfully!"
EOF

# Render the bash script by replacing placeholders with actual values from environment variables
for key in "${TO_RENDERED_KEYS[@]}"; do
    value="${!key}"
    BASH_SCRIPT="${BASH_SCRIPT//\$\{$key\}/$value}"
done

echo "Starting vitabench demo..."
echo "Bash Script:"
echo "========================================"
echo "$BASH_SCRIPT"
echo "========================================"

# Install rl-rock if not already available
if ! python -c "import rock" 2>/dev/null; then
    echo "Installing rl-rock..."
    pip install rl-rock
else
    echo "rl-rock is already installed."
fi

# Run the job via ROCK CLI
rock --base-url "$ROCK_BASE_URL" \
    --extra-header "XRL-Authorization=Bearer ${YOUR_API_KEY}" \
    --cluster "$ROCK_CLUSTER" \
    job run \
    --image "$ROCK_IMAGE" \
    --local-path "$LOCAL_WORKSPACE_DIR" \
    --target-path "$ROCK_WORKSPACE_DIR" \
    --timeout 3600 \
    --script-content "$BASH_SCRIPT"
