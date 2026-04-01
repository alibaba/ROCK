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
ROCK_IMAGE="${ROCK_IMAGE:-python:3.11}"
# =========================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Define the bash job script content
# This is the script that will be uploaded and executed inside the sandbox
read -r -d '' BASH_SCRIPT << 'EOF' || true
#!/bin/bash
echo "=== Simple Bash Job Demo ==="
echo "Hostname: $(hostname)"
echo "Date: $(date)"
echo "Python version: $(python3 --version 2>&1)"
echo "Working directory: $(pwd)"
echo ""
echo "Running a simple computation..."
for i in $(seq 1 5); do
    echo "  Step $i: processing..."
    sleep 1
done
echo ""
echo "Job completed successfully!"
EOF

# Create a temporary Python runner that invokes execute_script with the script above
PYTHON_RUNNER=$(mktemp /tmp/bash_job_demo_XXXXXX.py)
trap 'rm -f "$PYTHON_RUNNER"' EXIT

cat > "$PYTHON_RUNNER" << PYEOF
import asyncio
import sys
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

BASH_SCRIPT = '''${BASH_SCRIPT}'''

async def main():
    sandbox = Sandbox(
        SandboxConfig(
            image="${ROCK_IMAGE}",
            base_url="${ROCK_BASE_URL}",
            extra_headers={"XRL-Authorization": "Bearer ${YOUR_API_KEY}"},
            user_id="${YOUR_USER_ID}",
            experiment_id="${YOUR_EXPERIMENT_ID}",
        )
    )
    await sandbox.start()
    print(f"Sandbox ID: {sandbox.sandbox_id}")
    try:
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
