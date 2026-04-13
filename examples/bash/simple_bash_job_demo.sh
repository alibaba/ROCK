#!/bin/bash
# Simple bash job demo using `rock job run`
#
# This script defines a bash job inline and executes it inside a ROCK sandbox
# via the `rock job run` CLI command.

set -euo pipefail

# ===== Configuration =====
# Override via environment variables: ROCK_BASE_URL, YOUR_API_KEY, YOUR_USER_ID, YOUR_EXPERIMENT_ID
ROCK_BASE_URL="${ROCK_BASE_URL}"
YOUR_API_KEY="${YOUR_API_KEY}"
YOUR_USER_ID="${YOUR_USER_ID}"
YOUR_EXPERIMENT_ID="${YOUR_EXPERIMENT_ID:-simple_bash}"
ROCK_IMAGE="${ROCK_IMAGE:-rl-rock-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11}"
ROCK_CLUSTER="${ROCK_CLUSTER:-vpc-sg-sl-a}"

LOCAL_WORKSPACE_DIR="${LOCAL_WORKSPACE_DIR:-}"
ROCK_WORKSPACE_DIR="${ROCK_WORKSPACE_DIR:-/root/workspace}"

EXTERNAL_VARIABLE_1="external_value"
TO_RENDERED_KEYS=(
    "EXTERNAL_VARIABLE_1"
)
# =========================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Define the bash job script content
# This is the script that will be uploaded and executed inside the sandbox
read -r -d '' BASH_SCRIPT << 'EOF' || true
#!/bin/bash
echo "=== Simple Bash Job Demo ==="
echo ""
echo "Hostname: $(hostname)"
echo "Date: $(date)"
echo ""

# --- Pattern 1: Internal Variables ---
INTERNAL_VARIABLE_1="internal_value"
echo "--- Internal Variables ---"
echo "INTERNAL_VARIABLE_1: ${INTERNAL_VARIABLE_1}"
echo ""

# --- Pattern 2: External Variables (rendered into script content) ---
echo "--- External Variables (rendered) ---"
echo "EXTERNAL_VARIABLE_1: ${EXTERNAL_VARIABLE_1}"
echo ""

echo "Running a simple computation..."
for i in $(seq 1 5); do
    echo "  Step $i: processing..."
    sleep 1
done
echo ""
echo "Job completed successfully!"
EOF

# Render the bash script by replacing placeholders with actual values from environment variables
for key in "${TO_RENDERED_KEYS[@]}"; do
    value="${!key}"
    BASH_SCRIPT="${BASH_SCRIPT//\$\{$key\}/$value}"
done

echo "Starting simple bash job demo..."
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
run_args=(
    --base-url "$ROCK_BASE_URL"
    --extra-header "XRL-Authorization=Bearer ${YOUR_API_KEY}"
    --cluster "$ROCK_CLUSTER"
    job run
    --image "$ROCK_IMAGE"
    --timeout 3600
    --script-content "$BASH_SCRIPT"
)

if [ -n "$LOCAL_WORKSPACE_DIR" ]; then
    run_args+=(--local-path "$LOCAL_WORKSPACE_DIR" --target-path "$ROCK_WORKSPACE_DIR")
fi

rock "${run_args[@]}"
