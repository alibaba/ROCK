#!/bin/bash
# =============================================================================
# 来源: Agent-Hub/task/harbor/proxy-sidecar.sh（177 行完整脚本，未做任何内容改动）
# 用途: ROCK ComposeJobConfig 端到端用例 — cc-proxy sidecar
#
# 监听端口: 8082（见 PORT=8082，第 134 行）
# ComposeJobConfig 中声明: sidecars[].health.port = 8082
# 主容器通过 http://${DOCKER_GATEWAY}:8082 访问（动态获取 docker bridge gateway IP）
# =============================================================================
# harbor-v2/proxy-sidecar.sh — Claude-code proxy sidecar.
# Self-detects if proxy is needed; sleeps if not.


# Mounted volumes — read AP-injected env vars, with local-docker fallback.
# DO NOT override OUTPUT_DIR with a hardcoded path; that breaks the AP uploader.
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/output}"
SHARED_DIR="${SHARED_DIR:-/tmp/shared}"
export OUTPUT_DIR SHARED_DIR

MODEL_API_KEY="${MODEL_API_KEY:-$API_KEY}"
MODEL_BASE_URL="${MODEL_BASE_URL:-$BASE_URL}"

# ── Determine if proxy is needed ──
AGENT_LOWER=$(echo "${HARBOR_AGENT}" | tr '[:upper:]' '[:lower:]')
PROVIDER_LOWER=$(echo "${PROVIDER:-}" | tr '[:upper:]' '[:lower:]')
URL_LOWER=$(echo "${MODEL_BASE_URL:-}" | tr '[:upper:]' '[:lower:]')

use_claude_proxy=false
use_opencode_proxy=false
use_openclaw_proxy=false

if [ "$AGENT_LOWER" = "claude-code" ]; then
  if [ "${FORCE_PROXY}" = "true" ]; then
    use_claude_proxy=true
  elif [ "$PROVIDER_LOWER" != "anthropic" ] && [[ "$URL_LOWER" != *"anthropic"* ]]; then
    use_claude_proxy=true
  fi
fi

if [ "$AGENT_LOWER" = "opencode" ] && [ "${FORCE_PROXY}" = "true" ] && [ "$PROVIDER_LOWER" = "anthropic" ]; then
  use_opencode_proxy=true
fi

# Kilo-code uses the same proxy mechanism as opencode
use_kilocode_proxy=false
if [ "$AGENT_LOWER" = "kilo-code" ] && [ "${FORCE_PROXY}" = "true" ] && [ "$PROVIDER_LOWER" = "anthropic" ]; then
  use_kilocode_proxy=true
fi

if [ "$AGENT_LOWER" = "openclaw" ]; then
  if [ "${FORCE_PROXY}" = "true" ]; then
    use_openclaw_proxy=true
  elif [ "$PROVIDER_LOWER" != "anthropic" ] && [[ "$URL_LOWER" != *"anthropic"* ]]; then
    use_openclaw_proxy=true
  fi
fi

echo "AGENT_LOWER: $AGENT_LOWER"
echo "FORCE_PROXY: ${FORCE_PROXY}"
echo "PROVIDER_LOWER: $PROVIDER_LOWER"
echo "use_claude_proxy: $use_claude_proxy"
echo "use_opencode_proxy: $use_opencode_proxy"
echo "use_kilocode_proxy: $use_kilocode_proxy"
echo "use_openclaw_proxy: $use_openclaw_proxy"

if [ "$use_claude_proxy" = "false" ] && [ "$use_opencode_proxy" = "false" ] && [ "$use_kilocode_proxy" = "false" ] && [ "$use_openclaw_proxy" = "false" ]; then
  echo "Proxy not needed for agent=${HARBOR_AGENT}, sleeping..."
  exec sleep infinity
fi

echo "Starting claude-code proxy for agent=${HARBOR_AGENT}..."

# ── Map Agent-Hub env vars to proxy env vars ──
OPENAI_API_KEY="${MODEL_API_KEY}"
OPENAI_BASE_URL="${MODEL_BASE_URL}"
OPENAI_MODEL="${MODEL}"

# Native Anthropic mode
USE_NATIVE_ANTHROPIC="false"
ANTHROPIC_NATIVE_API_KEY=""
ANTHROPIC_NATIVE_BASE_URL=""

if [ "$use_claude_proxy" = "true" ] && [ "${FORCE_PROXY}" = "true" ]; then
  if [ "${NATIVE_ANTHROPIC}" = "true" ] || [[ "$PROVIDER_LOWER" == *"anthropic"* ]]; then
    USE_NATIVE_ANTHROPIC="true"
    ANTHROPIC_NATIVE_API_KEY="${MODEL_API_KEY}"
    ANTHROPIC_NATIVE_BASE_URL="${MODEL_BASE_URL}"
  fi
elif [ "$use_opencode_proxy" = "true" ] || [ "$use_kilocode_proxy" = "true" ]; then
  USE_NATIVE_ANTHROPIC="true"
  ANTHROPIC_NATIVE_API_KEY="${MODEL_API_KEY}"
  ANTHROPIC_NATIVE_BASE_URL="${MODEL_BASE_URL}"
elif [ "$use_openclaw_proxy" = "true" ]; then
  if [ "${NATIVE_ANTHROPIC}" = "true" ] || [[ "$PROVIDER_LOWER" == *"anthropic"* ]]; then
    USE_NATIVE_ANTHROPIC="true"
    ANTHROPIC_NATIVE_API_KEY="${MODEL_API_KEY}"
    ANTHROPIC_NATIVE_BASE_URL="${MODEL_BASE_URL}"
  fi
fi

# Force thinking for opencode + anthropic
THINKING_FORCE_ENABLED="false"
if [ "$use_opencode_proxy" = "true" ] || [ "$use_kilocode_proxy" = "true" ] || [ "$use_openclaw_proxy" = "true" ]; then
  THINKING_FORCE_ENABLED="true"
fi

# Thinking parameters
THINKING_ENABLED="${INTERLEAVED_THINKING:-false}"
THINKING_MODE="${THINKING_TYPE:-enabled}"
THINKING_BUDGET_TOKENS="${REASONING_BUDGET_TOKENS:-63000}"

ADD_EFFORT_PARAM="false"
EFFORT_TO_SET="high"
if [ -n "${REASONING_EFFORT:-}" ]; then
  ADD_EFFORT_PARAM="true"
  EFFORT_TO_SET="${REASONING_EFFORT}"
fi

ADD_1M_CONTEXT="${CONTEXT_1M:-false}"
USE_SYSTEM_HINT="${USE_HACK:-false}"
MAX_TOKENS_LIMIT="${MAX_TOKENS:-18000}"
MIN_TOKENS_LIMIT="${MAX_TOKENS:-18000}"

# ── Write proxy .env ──
cd /tmp/claude-code-proxy/
cat <<EOF > .env
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENAI_BASE_URL=${OPENAI_BASE_URL}
BIG_MODEL=${OPENAI_MODEL}
MIDDLE_MODEL=${MIDDLE_MODEL:-${OPENAI_MODEL}}
MIDDLE_OPENAI_API_KEY=${MIDDLE_OPENAI_API_KEY:-${OPENAI_API_KEY}}
MIDDLE_OPENAI_BASE_URL=${MIDDLE_OPENAI_BASE_URL:-${OPENAI_BASE_URL}}
SMALL_MODEL=${SMALL_MODEL:-${OPENAI_MODEL}}
SMALL_OPENAI_API_KEY=${SMALL_OPENAI_API_KEY:-${OPENAI_API_KEY}}
SMALL_OPENAI_BASE_URL=${SMALL_OPENAI_BASE_URL:-${OPENAI_BASE_URL}}
FORCE_OPENAI_NON_STREAMING=true
NON_STREAMING_RETRY_ENABLED=true
NON_STREAMING_RETRY_ATTEMPTS=10
NON_STREAMING_RETRY_DELAY=10
NON_STREAMING_RETRY_BACKOFF=2.0
HOST=0.0.0.0
PORT=8082
LOG_LEVEL=INFO
REQUEST_LOGGING_ENABLED=false
RESPONSE_LOGGING_ENABLED=false
REQUEST_LOG_LEVEL=INFO
PAIR_LOGGING_ENABLED=true
JSON_LOG_ENABLED=true
JSON_LOG_DIR=logs
MAX_TOKENS_LIMIT=${MAX_TOKENS_LIMIT}
MIN_TOKENS_LIMIT=${MIN_TOKENS_LIMIT}
REQUEST_TIMEOUT=${REQUEST_TIMEOUT:-3600}
MAX_RETRIES=10
TEMPERATURE=${TEMPERATURE}
ANTHROPIC_NATIVE_API_KEY=${ANTHROPIC_NATIVE_API_KEY:-}
ANTHROPIC_NATIVE_BASE_URL=${ANTHROPIC_NATIVE_BASE_URL:-}
USE_NATIVE_ANTHROPIC=${USE_NATIVE_ANTHROPIC:-false}
THINKING_ENABLED=${THINKING_ENABLED:-false}
THINKING_FORCE_ENABLED=${THINKING_FORCE_ENABLED:-false}
THINKING_MODE=${THINKING_MODE:-enabled}
THINKING_BUDGET_TOKENS=${THINKING_BUDGET_TOKENS:-63000}
ADD_EFFORT_PARAM=${ADD_EFFORT_PARAM:-false}
EFFORT_TO_SET=${EFFORT_TO_SET:-high}
ADD_1M_CONTEXT=${ADD_1M_CONTEXT:-false}
USE_SYSTEM_HINT=${USE_SYSTEM_HINT:-false}
REASONING_SPLIT=${REASONING_SPLIT:-false}
TOP_P=${TOP_P:-none}
EOF

# Append custom proxy env vars (JSON)
if [ -n "${PROXY_ENVS:-}" ]; then
  echo "$PROXY_ENVS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for k, v in d.items():
    print(f'{k}={v}')
" >> .env 2>/dev/null || true
fi

mkdir -p ${OUTPUT_DIR}/claudecode_logs
# create a softlink for cc logs sync
ln -s ${OUTPUT_DIR}/claudecode_logs /tmp/claude-code-proxy/logs
# validate:
ls -la /tmp/claude-code-proxy/
python /tmp/claude-code-proxy/start_proxy.py 2>&1 | tee ${OUTPUT_DIR}/claudecode_logs/proxy_stdout.log
