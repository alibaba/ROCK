#!/bin/bash
# Simple bash job demo using Sandbox.process.execute_script
#
# This script defines a bash job inline and executes it inside a ROCK sandbox
# via sandbox.process.execute_script().

set -euo pipefail

# ===== Configuration =====
# Override via environment variables: YOUR_API_KEY, YOUR_USER_ID, YOUR_EXPERIMENT_ID
ROCK_BASE_URL="${ROCK_BASE_URL}"
YOUR_API_KEY="${YOUR_API_KEY}"
YOUR_USER_ID="${YOUR_USER_ID}"
YOUR_EXPERIMENT_ID="${YOUR_EXPERIMENT_ID}"
ROCK_IMAGE="${ROCK_IMAGE:-rl-rock-registry-vpc.ap-southeast-1.cr.aliyuncs.com/chatos/base:python3.11}"
ROCK_CLUSTER="${ROCK_CLUSTER:-vpc-sg-sl-a}"
LOCAL_WORKSPACE_DIR="${LOCAL_WORKSPACE_DIR:-/root/code/TAU-Bench/vitabench}"

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

# Create a temporary Python runner that invokes execute_script with the script above
PYTHON_RUNNER=$(mktemp /tmp/bash_job_demo_XXXXXX.py)
trap 'rm -f "$PYTHON_RUNNER"' EXIT

cat > "$PYTHON_RUNNER" << PYEOF
import asyncio
import sys
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

BASH_SCRIPT = '''${BASH_SCRIPT}'''
LOCAL_WORKSPACE_DIR = "${LOCAL_WORKSPACE_DIR}"
ROCK_WORKSPACE_DIR = "${ROCK_WORKSPACE_DIR}"

async def main():
    sandbox = Sandbox(
        SandboxConfig(
            image="${ROCK_IMAGE}",
            base_url="${ROCK_BASE_URL}",
            extra_headers={"XRL-Authorization": "Bearer ${YOUR_API_KEY}"},
            user_id="${YOUR_USER_ID}",
            experiment_id="${YOUR_EXPERIMENT_ID}",
            cluster="${ROCK_CLUSTER}",
        )
    )
    await sandbox.start()
    print(f"Sandbox ID: {sandbox.sandbox_id}")
    try:
        if LOCAL_WORKSPACE_DIR:
            print(f"Uploading {LOCAL_WORKSPACE_DIR} -> {ROCK_WORKSPACE_DIR}")
            result = await sandbox.fs.upload_dir(
                source_dir=LOCAL_WORKSPACE_DIR,
                target_dir=ROCK_WORKSPACE_DIR,
            )
            if result.exit_code != 0:
                print(f"Upload failed: {result.failure_reason}")
                sys.exit(1)
            print(f"Upload succeeded: {result.output}")

        result = await sandbox.process.execute_script(
            script_content=BASH_SCRIPT,
            script_name="simple_bash_job.sh",
            wait_timeout=60,
        )
        print(f"Exit code: {result.exit_code}")
        print(f"Output:\n{result.output}")
        if result.exit_code != 0:
            sys.exit(1)
    finally:
        await sandbox.stop()

if __name__ == "__main__":
    asyncio.run(main())
PYEOF

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

python "$PYTHON_RUNNER"
