#!/bin/bash
# =============================================================================
# 来源: Agent-Hub/task/harbor/main.sh（1055 行完整脚本）
# 用途: ROCK ComposeJobConfig 端到端用例 — harbor 任务 (claude-code / terminal-bench)
#
# 改动说明（相对原始文件）:
#   1. 注释头更新（本注释块），余下内容原封不动。
#
# 运行方案: 主容器自带 dockerd（DinD 沙箱 image=docker:27-dind 天然提供），
#           DOCKER_HOST 保持 tcp://localhost:2375（见第 17 行），即指向外层 DinD 守护进程。
#           注意：附录 B 的翻译中 DOCKER_HOST 改成了 tcp://docker-daemon:2375
#           （因为那是三层嵌套 DinD + 独立 dockerd sidecar 的方案），
#           但本端到端用例采用"外层 DinD 沙箱自带 dockerd"方案，无独立 docker-daemon sidecar，
#           所以 DOCKER_HOST 继续指向 localhost:2375。
#
# proxy 访问: main.sh 里所有对 proxy 的访问都通过 DOCKER_GATEWAY 动态获取
#             （`http://${DOCKER_GATEWAY}:8082`），并非硬编码 127.0.0.1:8082，
#             因此在 ComposeJobConfig 场景下无需修改。
#             proxy sidecar 以 network-alias=proxy 运行，主容器通过 docker bridge
#             网关访问，端口 8082 不变。
# =============================================================================
# harbor/main.sh — Harbor agent runner for Agent-Hub.
# Supports: terminus-2, claude-code, opencode, openclaw, codex, aider, goose, etc.
set -e

# ── Environment setup ──

# Mounted volumes — read AP-injected env vars, with local-docker fallback.
# DO NOT override OUTPUT_DIR with a hardcoded path; that breaks the AP uploader.
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/output}"
SHARED_DIR="${SHARED_DIR:-/tmp/shared}"
export OUTPUT_DIR SHARED_DIR

MODEL_API_KEY="${MODEL_API_KEY:-$API_KEY}"
MODEL_BASE_URL="${MODEL_BASE_URL:-$BASE_URL}"

# ── ROCK ComposeJobConfig 适配（相对原始 Agent-Hub main.sh 的唯一逻辑改动）──
# 原始脚本假设有个 docker-daemon sidecar 在 tcp://localhost:2375 提供 dockerd（K8s 同 Pod 模型）。
# 在 ComposeJobConfig 的"两层"方案里，外层 ROCK kata 沙箱已由 runner.sh 启动 dockerd，
# 并通过 volume_mount(host_path=/var/run/docker.sock) 把外层 socket 挂进本主容器。
# 因此优先复用外层 dockerd（避免在主容器内再起第三层 dockerd，那在 kata 下会失败）。
if [ -S /var/run/docker.sock ]; then
  export DOCKER_HOST="unix:///var/run/docker.sock"
  echo "[rock-compose-adapt] reusing outer dockerd via mounted /var/run/docker.sock"
else
  export DOCKER_HOST="tcp://localhost:2375"
fi
export OPENAI_API_KEY="${MODEL_API_KEY}"
export OPENAI_BASE_URL="${MODEL_BASE_URL}"
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-${MODEL_BASE_URL}}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-${MODEL_API_KEY}}"

# cc-proxy sidecar: per-model overrides for BIG/MIDDLE/SMALL.
# All fall back to the main model credentials when not provided.
# Keep in sync with proxy-sidecar.sh .env rendering and with
# Agent-Service harbor plugin.py env injection.
export MIDDLE_MODEL="${MIDDLE_MODEL:-${MODEL}}"
export SMALL_MODEL="${SMALL_MODEL:-${MODEL}}"
export MIDDLE_OPENAI_API_KEY="${MIDDLE_OPENAI_API_KEY:-${OPENAI_API_KEY}}"
export MIDDLE_OPENAI_BASE_URL="${MIDDLE_OPENAI_BASE_URL:-${OPENAI_BASE_URL}}"
export SMALL_OPENAI_API_KEY="${SMALL_OPENAI_API_KEY:-${OPENAI_API_KEY}}"
export SMALL_OPENAI_BASE_URL="${SMALL_OPENAI_BASE_URL:-${OPENAI_BASE_URL}}"

if [ "${HARBOR_AGENT}" = "openclaw" ]; then
  export MODEL_REASONING="true"
  export MODEL_CONTEXT_LENGTH="${MODEL_CONTEXT_LENGTH:-131072}"
  export MODEL_MAX_TOKENS="${MAX_TOKENS:-${MODEL_MAX_TOKENS:-8192}}"
  export OPENCLAW_NODE_VERSION="${NODE_VERSION:-24.14.0}"
  export HARBOR_NPM_INSTALL_MODE="${HARBOR_NPM_INSTALL_MODE:-mirror-preferred}"
  export HARBOR_NPM_REGISTRY_PORT="${HARBOR_NPM_REGISTRY_PORT:-14873}"
  export HARBOR_NODE_DIST_PORT="${HARBOR_NODE_DIST_PORT:-14874}"
  if [ -n "${TEMPERATURE:-}" ]; then
    export MODEL_TEMPERATURE="${TEMPERATURE}"
  fi
fi

echo "Starting Harbor agent execution..."
echo "Harbor Agent: ${HARBOR_AGENT}"
echo "Dataset Type: ${DATASET_TYPE}"
if [ "${DATASET_TYPE}" = "registry" ]; then
  echo "Dataset Name: ${DATASET_NAME}"
  echo "Dataset Version: ${DATASET_VERSION}"
fi
echo "Task ID: ${INSTANCE_ID}"
echo "Model: ${MODEL}"
echo "Environment: ${HARBOR_ENV}"
if [ "${HARBOR_AGENT}" = "openclaw" ]; then
  echo "OpenClaw version: ${AGENT_VERSION:-latest}"
  echo "OpenClaw thinking_level: ${THINKING_LEVEL}"
fi

HARBOR_JOB_RESULT_PATH="${HARBOR_JOB_RESULT_PATH:-${SHARED_DIR}/rollout_result}"
mkdir -p "${HARBOR_JOB_RESULT_PATH}"

# ── Wait for docker daemon (DinD sidecar) ──
echo "Waiting for docker daemon..."
max_attempts=30
attempt=0
while [ $attempt -lt $max_attempts ]; do
  if docker ps >/dev/null 2>&1; then
    echo "Docker daemon is ready"
    break
  fi
  attempt=$((attempt + 1))
  echo "Waiting for docker daemon... (attempt $attempt/$max_attempts)"
  sleep 2
done

if [ $attempt -eq $max_attempts ]; then
  echo "ERROR: Docker daemon failed to start after $max_attempts attempts"
  exit 1
fi

docker info || { echo "ERROR: Docker info command failed"; exit 1; }
export DOCKER_BUILDKIT=0

DOCKER_GATEWAY=""

if [ "${HARBOR_AGENT}" = "openclaw" ]; then
  DOCKER_GATEWAY=$(docker network inspect bridge --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)
  echo "DOCKER_GATEWAY IP is ${DOCKER_GATEWAY}"
  export HARBOR_NPM_REGISTRY_URL="http://${DOCKER_GATEWAY}:${HARBOR_NPM_REGISTRY_PORT}"
  export HARBOR_NODE_DIST_BASE_URL="http://${DOCKER_GATEWAY}:${HARBOR_NODE_DIST_PORT}"

  echo "Waiting for npm mirror sidecar..."
  max_attempts=30
  attempt=0
  while [ $attempt -lt $max_attempts ]; do
    if curl -s --connect-timeout 1 "http://localhost:${HARBOR_NPM_REGISTRY_PORT}" >/dev/null 2>&1; then
      echo "NPM mirror is ready"
      break
    fi
    attempt=$((attempt + 1))
    echo "Waiting for npm mirror... (attempt $attempt/$max_attempts)"
    sleep 2
  done
fi

# ── Download dataset from OSS if not present locally (non-registry) ──
if [ "${DATASET_TYPE}" != "registry" ] && [ ! -d "./${INSTANCE_ID}" ]; then
  if [ -z "${DATASET}" ] || [ -z "${SPLIT}" ]; then
    echo "ERROR: DATASET and SPLIT must be set for OSS download"
    exit 1
  fi
  DATASET_KEY="${DATASET}/${SPLIT}"

  cat > /root/.ossutilconfig <<EOF
[Credentials]
language=CH
endpoint=$OSS_ENDPOINT
accessKeyID=$OSS_ACCESS_KEY_ID
accessKeySecret=$OSS_ACCESS_KEY_SECRET
EOF

  DATA_OSS_PATH=oss://$OSS_BUCKET/swe/datasets/${DATASET_KEY}-assets/${INSTANCE_ID}
  echo "Downloading dataset from ${DATA_OSS_PATH}..."
  if ossutil stat ${DATA_OSS_PATH}/content.tgz >/dev/null 2>&1; then
      mkdir -p ./${INSTANCE_ID} && cd ./${INSTANCE_ID}
      ossutil cp -f $DATA_OSS_PATH/content.tgz ./content.tgz --retry-times=500
      tar -zxf content.tgz >/dev/null 2>&1
      rm -rf content.tgz
      cd -
  else
      mkdir -p ./${INSTANCE_ID}
      ossutil cp -r -f $DATA_OSS_PATH ./${INSTANCE_ID} --retry-times=500
  fi
  echo "Dataset downloaded for ${INSTANCE_ID}"

  # Flatten nested directory if exists
  if [ -d ./${INSTANCE_ID}/${INSTANCE_ID} ]; then
      cp -r ./${INSTANCE_ID}/${INSTANCE_ID}/* ./${INSTANCE_ID}/
  fi
fi

# ── Load pre-baked task base image ──
echo "Checking for task base image..."
TASK_IMAGE_JSON="/images/task-image.json"
if [ -f "${TASK_IMAGE_JSON}" ]; then
  echo "Found task-image.json, checking for task ${INSTANCE_ID}..."
  TAR_FILE=$(python3 -c "import json; data = json.load(open('${TASK_IMAGE_JSON}')); print(data.get('${INSTANCE_ID}', {}).get('tar_file', ''))" 2>/dev/null)
  if [ -n "${TAR_FILE}" ]; then
    TAR_PATH="/images/${TAR_FILE}"
    if [ -f "${TAR_PATH}" ]; then
      echo "Loading base image from ${TAR_FILE}..."
      docker load -i "${TAR_PATH}" || echo "Warning: Failed to load base image from ${TAR_FILE}"
    else
      echo "Tar file not found: ${TAR_PATH}"
    fi
  else
    echo "No pre-baked image for task ${INSTANCE_ID}"
  fi
else
  echo "No task-image.json found, skipping image loading"
fi

# ── Opencode: generate provider config ──
if [ "${HARBOR_AGENT}" = "opencode" ]; then
  echo "/"
  # Determine npm package and endpoint based on provider
  if [ "${PROVIDER}" = "anthropic" ]; then
    OPENCODE_NPM="@ai-sdk/anthropic"
    OPENCODE_BASE_URL="${ANTHROPIC_BASE_URL}"
    OPENCODE_API_KEY="${ANTHROPIC_API_KEY}"
  else
    OPENCODE_NPM="@ai-sdk/openai-compatible"
    OPENCODE_BASE_URL="${OPENAI_BASE_URL}"
    OPENCODE_API_KEY="${OPENAI_API_KEY}"
    unset OPENAI_API_KEY
  fi

  # When using proxy with anthropic, route through docker gateway
  if [ "${FORCE_PROXY}" = "true" ] && [ "${PROVIDER}" = "anthropic" ]; then
    echo "Waiting for claude-code proxy (opencode + anthropic + force_proxy)..."
    if command -v curl > /dev/null 2>&1; then
      while true; do
        curl -s --connect-timeout 1 http://localhost:8082 > /dev/null 2>&1 && break
        sleep 1
      done
    else
      sleep 60
    fi

    DOCKER_GATEWAY=$(docker network inspect bridge --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)
    if [ -n "${DOCKER_GATEWAY}" ]; then
      OPENCODE_BASE_URL="http://${DOCKER_GATEWAY}:8082/v1"
      OPENCODE_API_KEY="any-value"
    fi
  fi

  # Thinking params — skip if proxy handles it (force_proxy + anthropic)
  OPENCODE_THINKING="${INTERLEAVED_THINKING:-}"
  OPENCODE_THINKING_TYPE="${THINKING_TYPE:-}"
  OPENCODE_EFFORT="${REASONING_EFFORT:-}"
  OPENCODE_BUDGET="${REASONING_BUDGET_TOKENS:-}"
  OPENCODE_CUSTOM_OPTIONS="${OPENCODE_OPTIONS:-}"
  if [ "${FORCE_PROXY}" = "true" ] && [ "${PROVIDER}" = "anthropic" ]; then
    OPENCODE_THINKING=""
    OPENCODE_THINKING_TYPE=""
    OPENCODE_EFFORT=""
    OPENCODE_BUDGET=""
  fi

  # Generate opencode.json
  python3 - "${OPENCODE_NPM}" "${PROVIDER}" "${OPENCODE_BASE_URL}" "${OPENCODE_API_KEY}" "${MODEL}" "${OPENCODE_THINKING}" "${OPENCODE_THINKING_TYPE}" "${OPENCODE_EFFORT}" "${OPENCODE_BUDGET}" "${OPENCODE_CUSTOM_OPTIONS}" "${TEMPERATURE}" << 'PYEOF'
import sys, json, os

npm, provider, base_url, api_key, model = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
thinking_raw = sys.argv[6] if len(sys.argv) > 6 else ""
thinking_type_raw = sys.argv[7] if len(sys.argv) > 7 else ""
effort_raw = sys.argv[8] if len(sys.argv) > 8 else ""
budget_raw = sys.argv[9] if len(sys.argv) > 9 else ""
custom_options_raw = sys.argv[10] if len(sys.argv) > 10 else ""
temperature_raw = sys.argv[11] if len(sys.argv) > 11 else ""

custom_options = {}
if custom_options_raw:
    try:
        custom_options = json.loads(custom_options_raw)
    except (json.JSONDecodeError, ValueError):
        pass

is_anthropic = "anthropic" in provider.lower() or "claude" in model.lower()
thinking_enabled = bool(thinking_raw) and thinking_raw.lower() not in ("false", "0", "no", "disabled")

model_options = {}
if thinking_enabled:
    if is_anthropic:
        if thinking_type_raw:
            thinking_type = thinking_type_raw
        elif thinking_raw.lower() not in ("true", "1", "yes"):
            thinking_type = thinking_raw
        else:
            thinking_type = "adaptive"
        if thinking_type == "enabled":
            budget = int(budget_raw) if budget_raw else 10000
            model_options = {"thinking": {"type": "enabled", "budgetTokens": budget}}
        else:
            model_options = {"thinking": {"type": thinking_type}}
        if effort_raw:
            model_options["effort"] = effort_raw
    else:
        model_options = {"enable_thinking": True}

# Add reasoningEffort for non-anthropic providers when effort is specified.
# Mirror of Agent-Service harbor plugin (commit 6c012cd14): allow effort
# to take effect even when thinking flag was not explicitly turned on.
if effort_raw and not is_anthropic:
    model_options["enable_thinking"] = True
    model_options["reasoningEffort"] = effort_raw

if custom_options:
    model_options.update(custom_options)

# Build config
config = {
    "$schema": "https://opencode.ai/config.json",
    "snapshot": False,
    "permission": "allow",
}

if temperature_raw:
    temp = float(temperature_raw)
    config["agent"] = {
        k: {"temperature": temp}
        for k in ["build", "plan", "general", "explore", "title", "summary", "compaction"]
    }

model_entry = {"name": model}
if model_options:
    model_entry["options"] = model_options

config["provider"] = {
    provider: {
        "npm": npm,
        "name": provider,
        "options": {
            "baseURL": base_url,
            "apiKey": api_key,
        },
        "models": {
            model: model_entry,
        },
    }
}

shared_dir = os.environ["SHARED_DIR"]
os.makedirs(shared_dir, exist_ok=True)
with open(f"{shared_dir}/opencode.json", "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
PYEOF
  export OPENCODE_CONFIG_PATH="${SHARED_DIR}/opencode.json"
  echo "Opencode config written to ${OPENCODE_CONFIG_PATH}, the content is:"
  cat "${OPENCODE_CONFIG_PATH}"
fi

# ── Kilo-code: generate provider config ──
if [ "${HARBOR_AGENT}" = "kilo-code" ]; then
  # Determine base URL and API key based on provider
  if [ "${PROVIDER}" = "anthropic" ]; then
    KILO_BASE_URL="${ANTHROPIC_BASE_URL}"
    KILO_API_KEY="${ANTHROPIC_API_KEY}"
  else
    KILO_BASE_URL="${OPENAI_BASE_URL}"
    KILO_API_KEY="${OPENAI_API_KEY}"
  fi

  # When using proxy with anthropic, route through docker gateway
  if [ "${FORCE_PROXY}" = "true" ] && [ "${PROVIDER}" = "anthropic" ]; then
    echo "Waiting for claude-code proxy (kilo-code + anthropic + force_proxy)..."
    if command -v curl > /dev/null 2>&1; then
      while true; do
        curl -s --connect-timeout 1 http://localhost:8082 > /dev/null 2>&1 && break
        sleep 1
      done
    else
      sleep 60
    fi

    DOCKER_GATEWAY=$(docker network inspect bridge --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)
    if [ -n "${DOCKER_GATEWAY}" ]; then
      KILO_BASE_URL="http://${DOCKER_GATEWAY}:8082/v1"
      KILO_API_KEY="any-value"
    fi
  fi

  # Thinking params — skip if proxy handles it (force_proxy + anthropic)
  KILO_TEMPERATURE="${TEMPERATURE:-}"
  KILO_THINKING="${INTERLEAVED_THINKING:-}"
  KILO_THINKING_TYPE="${THINKING_TYPE:-}"
  KILO_EFFORT="${REASONING_EFFORT:-}"
  KILO_BUDGET="${REASONING_BUDGET_TOKENS:-}"
  if [ "${FORCE_PROXY}" = "true" ] && [ "${PROVIDER}" = "anthropic" ]; then
    KILO_THINKING=""
    KILO_THINKING_TYPE=""
    KILO_EFFORT=""
    KILO_BUDGET=""
  fi

  # Generate kilo.json
  python3 - "${PROVIDER}" "${MODEL}" "${KILO_BASE_URL}" "${KILO_API_KEY}" "${KILO_TEMPERATURE}" "${KILO_THINKING}" "${KILO_THINKING_TYPE}" "${KILO_EFFORT}" "${KILO_BUDGET}" << 'PYEOF'
import sys, json, os

provider = sys.argv[1]
model = sys.argv[2]
base_url = sys.argv[3] if len(sys.argv) > 3 else ""
api_key = sys.argv[4] if len(sys.argv) > 4 else ""
temperature_raw = sys.argv[5] if len(sys.argv) > 5 else ""
thinking_raw = sys.argv[6] if len(sys.argv) > 6 else ""
thinking_type_raw = sys.argv[7] if len(sys.argv) > 7 else ""
effort_raw = sys.argv[8] if len(sys.argv) > 8 else ""
budget_raw = sys.argv[9] if len(sys.argv) > 9 else ""

is_anthropic = "anthropic" in provider.lower() or "claude" in model.lower()

model_options = {}

# Thinking
thinking_enabled = bool(thinking_raw) and thinking_raw.lower() not in ("false", "0", "no", "disabled")
if thinking_enabled:
    if is_anthropic:
        if thinking_type_raw:
            thinking_type = thinking_type_raw
        elif thinking_raw.lower() not in ("true", "1", "yes"):
            thinking_type = thinking_raw
        else:
            thinking_type = "adaptive"
        if thinking_type == "enabled":
            budget = int(budget_raw) if budget_raw else 10000
            model_options["thinking"] = {"type": "enabled", "budgetTokens": budget}
        else:
            model_options["thinking"] = {"type": thinking_type}
        if effort_raw:
            model_options["effort"] = effort_raw
    else:
        model_options["enable_thinking"] = True

# Build provider config
model_entry = {}
if model_options:
    model_entry["options"] = model_options

provider_options = {}
if base_url:
    provider_options["baseURL"] = base_url
if api_key:
    provider_options["apiKey"] = api_key

provider_config = {"models": {model: model_entry}}
if provider_options:
    provider_config["options"] = provider_options

config = {"provider": {provider: provider_config}}

# Temperature is configured at agent level in kilocode
if temperature_raw:
    try:
        temp = float(temperature_raw)
        agent_config = {"temperature": temp}
        config["agent"] = {
            k: dict(agent_config)
            for k in ["general", "plan", "explore", "title", "summary"]
        }
    except ValueError:
        pass

shared_dir = os.environ["SHARED_DIR"]
os.makedirs(shared_dir, exist_ok=True)
with open(f"{shared_dir}/kilo.json", "w") as f:
    json.dump(config, f, indent=2)
print(f"Kilo config written to {shared_dir}/kilo.json: " + json.dumps(config))
PYEOF
  export KILO_CONFIG_PATH="${SHARED_DIR}/kilo.json"
  echo "Kilo config generated at ${KILO_CONFIG_PATH}"
fi

# ── Determine if proxy sidecar is in use ──
USE_PROXY=0
if [ "${HARBOR_AGENT}" = "claude-code" ]; then
  if [ "${FORCE_PROXY}" = "true" ]; then
    USE_PROXY=1
  elif [ "${PROVIDER}" != "anthropic" ]; then
    URL_LOWER=$(echo "${ANTHROPIC_BASE_URL:-}" | tr '[:upper:]' '[:lower:]')
    if [[ "${URL_LOWER}" != *"anthropic"* ]]; then
      USE_PROXY=1
    fi
  fi
elif { [ "${HARBOR_AGENT}" = "opencode" ] || [ "${HARBOR_AGENT}" = "kilo-code" ]; } && [ "${FORCE_PROXY}" = "true" ] && [ "${PROVIDER}" = "anthropic" ]; then
  USE_PROXY=1
elif [ "${HARBOR_AGENT}" = "openclaw" ]; then
  if [ "${FORCE_PROXY}" = "true" ]; then
    USE_PROXY=1
  elif [ -n "${PROVIDER}" ] && [ "${PROVIDER}" != "anthropic" ]; then
    URL_LOWER=$(echo "${ANTHROPIC_BASE_URL:-}" | tr '[:upper:]' '[:lower:]')
    if [[ "${URL_LOWER}" != *"anthropic"* ]]; then
      USE_PROXY=1
    fi
  fi
fi
echo "USE_PROXY=${USE_PROXY}"

# Wait for proxy if needed (claude-code only; opencode waits earlier)
if [ "${HARBOR_AGENT}" = "claude-code" ] && [ "${USE_PROXY}" = "1" ]; then
  echo "Waiting for claude-code proxy..."
  if command -v curl > /dev/null 2>&1; then
    while true; do
      curl -s --connect-timeout 1 http://localhost:8082 > /dev/null 2>&1 && break
      sleep 1
    done
  else
    sleep 60
  fi
fi

if [ "${HARBOR_AGENT}" = "openclaw" ] && [ "${USE_PROXY}" = "1" ]; then
  echo "Waiting for claude-code proxy..."
  if command -v curl > /dev/null 2>&1; then
    while true; do
      curl -s --connect-timeout 1 http://localhost:8082 > /dev/null 2>&1 && break
      sleep 1
    done
  else
    sleep 60
  fi
fi

if [ "${HARBOR_AGENT}" = "openclaw" ] && [ "${USE_PROXY}" = "1" ]; then
  if [ -z "${DOCKER_GATEWAY}" ]; then
    DOCKER_GATEWAY=$(docker network inspect bridge --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)
  fi
  export MODEL_BASE_URL="http://${DOCKER_GATEWAY}:8082"
  export ANTHROPIC_BASE_URL="${MODEL_BASE_URL}"
  export OPENAI_BASE_URL="${MODEL_BASE_URL}"
  export MODEL_API="anthropic-messages"
  export MODEL_API_KEY="sk-any-value"
fi

# ── Model name ──
if [ -n "${PROVIDER}" ]; then
  MODEL_NAME="${PROVIDER}/${MODEL}"
else
  MODEL_NAME="${MODEL}"
fi
echo "Model name: ${MODEL_NAME}"

# ── Claude-code: configure API endpoints and env ──
if [ "${HARBOR_AGENT}" = "claude-code" ]; then
  if [ -n "${ANTHROPIC_BASE_URL}" ]; then
    if [ "${USE_PROXY}" = "1" ]; then
      # Route through proxy via docker bridge gateway
      DOCKER_GATEWAY=$(docker network inspect bridge --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)
      if [ -n "${DOCKER_GATEWAY}" ]; then
        export ANTHROPIC_BASE_URL="http://${DOCKER_GATEWAY}:8082"
        export ANTHROPIC_API_KEY="any-value"
        echo "Claude-code proxy mode: ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}"
      fi
      # Strip provider prefix for proxy mode (aligns with Agent-Service main.j2 L594)
      MODEL_NAME="${MODEL}"
      echo "Updated MODEL_NAME to: ${MODEL_NAME}"
    else
      # Direct Anthropic API mode — auto-append /v1 if missing
      if [[ "${ANTHROPIC_BASE_URL}" != */v1 ]]; then
        ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL}/v1"
      fi
      echo "Claude-code native mode: ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}"
      export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL}"
      export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_API_KEY:-}"
      export ANTHROPIC_DEFAULT_OPUS_MODEL="${MODEL}"
      export ANTHROPIC_DEFAULT_SONNET_MODEL="${MODEL}"
      export ANTHROPIC_DEFAULT_HAIKU_MODEL="${MODEL}"
      export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
      MODEL_NAME="${MODEL}"
      unset ANTHROPIC_API_KEY
    fi
  fi

  if [ -n "${OPENAI_BASE_URL}" ]; then export OPENAI_BASE_URL="${OPENAI_BASE_URL}"; fi
  if [ -n "${OPENAI_API_KEY}" ]; then export OPENAI_API_KEY="${OPENAI_API_KEY}"; fi

  if [ -n "${MAX_TOKENS}" ]; then
    export CLAUDE_CODE_MAX_OUTPUT_TOKENS="${MAX_TOKENS}"
  fi

  mkdir -p ${OUTPUT_DIR}/claudecode_logs/
fi

# ── Local dataset: Dockerfile injections (non-registry) ──
if [ "${DATASET_TYPE}" != "registry" ]; then
  if [ "${HARBOR_AGENT}" = "claude-code" ] && [ -n "${ENABLE_TOOL_SEARCH}" ]; then
    TASK_ENV_DOCKERFILE="./${INSTANCE_ID}/environment/Dockerfile"
    if [ -f "${TASK_ENV_DOCKERFILE}" ]; then
      echo "ENV ENABLE_TOOL_SEARCH=${ENABLE_TOOL_SEARCH}" >> "${TASK_ENV_DOCKERFILE}"
    fi
  fi

  # Inject NODE_OPTIONS into task Dockerfile (opencode, memory optimization)
  if [ "${HARBOR_AGENT,,}" = "opencode" ] && [ "${ENABLE_NODE_MEMORY_OPTIMIZATION}" != "false" ] && [ -n "${OVERRIDE_MEMORY_MB}" ]; then
    TASK_ENV_DOCKERFILE="./${INSTANCE_ID}/environment/Dockerfile"
    if [ -f "${TASK_ENV_DOCKERFILE}" ]; then
      NODE_MAX_OLD_SPACE=$((OVERRIDE_MEMORY_MB / 2))
      JSC_RAM_SIZE=$((NODE_MAX_OLD_SPACE / 1024))
      echo "" >> "${TASK_ENV_DOCKERFILE}"
      echo "ENV NODE_OPTIONS=\"--max-old-space-size=${NODE_MAX_OLD_SPACE}\"" >> "${TASK_ENV_DOCKERFILE}"
      echo "ENV JSC_forceRAMSize=${JSC_RAM_SIZE}gb" >> "${TASK_ENV_DOCKERFILE}"
      export NODE_OPTIONS="--max-old-space-size=${NODE_MAX_OLD_SPACE}"
      export JSC_forceRAMSize="${JSC_RAM_SIZE}gb"
    fi
  fi

fi

# ══════════════════════════════════════════════════════════════════════
# LiteLLM sidecars: wait for readiness and rewrite env to route via proxy
# ══════════════════════════════════════════════════════════════════════

# ── Eval judge LiteLLM proxy (port 4000): wait, rewrite EVAL_* ──
if [ -n "${EVAL_API_KEY}" ] && [ -n "${EVAL_API_BASE}" ] && [ -n "${EVAL_MODEL}" ]; then
  echo "Waiting for LiteLLM proxy sidecar to be ready on port 4000..."
  for i in $(seq 1 30); do
    if (echo > /dev/tcp/localhost/4000) 2>/dev/null; then
      echo "LiteLLM proxy ready after ${i}s"
      break
    fi
    if [ $i -eq 30 ]; then
      echo "WARNING: LiteLLM proxy failed to start within 30s"
    fi
    sleep 1
  done

  if [ -z "${DOCKER_GATEWAY}" ]; then
    DOCKER_GATEWAY=$(docker network inspect bridge --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)
  fi
  if [ -n "${DOCKER_GATEWAY}" ]; then
    echo "Docker gateway detected: ${DOCKER_GATEWAY}"
    EVAL_MODEL_RAW="${EVAL_MODEL}"
    export EVAL_API_KEY="sk-litellm-local"
    export EVAL_API_BASE="http://${DOCKER_GATEWAY}:4000/v1"
    # Strip provider prefix from EVAL_MODEL (e.g. "anthropic/sonnet" -> "sonnet")
    if [[ "${EVAL_MODEL_RAW}" == */* ]]; then
      export EVAL_MODEL="${EVAL_MODEL_RAW#*/}"
    else
      export EVAL_MODEL="${EVAL_MODEL_RAW}"
    fi
    echo "Overridden EVAL_API_KEY to: sk-litellm-local"
    echo "Overridden EVAL_API_BASE to: ${EVAL_API_BASE}"
    echo "Overridden EVAL_MODEL to: ${EVAL_MODEL} (raw: ${EVAL_MODEL_RAW})"
  else
    echo "WARNING: Could not detect docker gateway, using original EVAL_* env vars"
  fi
fi

# ── Codex force_proxy: rewrite OPENAI_BASE_URL to the same sidecar (port 4000) ──
# The unified litellm-proxy sidecar serves both eval judge and codex groups in
# one process; codex just targets it on the same port as eval.
if [ "${HARBOR_AGENT}" = "codex" ] && [ "${FORCE_PROXY}" = "true" ]; then
  echo "Waiting for LiteLLM proxy sidecar (codex group) to be ready on port 4000..."
  for i in $(seq 1 60); do
    if (echo > /dev/tcp/localhost/4000) 2>/dev/null; then
      echo "LiteLLM proxy ready after ${i}s"
      break
    fi
    if [ $i -eq 60 ]; then
      echo "WARNING: LiteLLM proxy failed to start within 60s"
    fi
    sleep 1
  done

  if [ -z "${DOCKER_GATEWAY}" ]; then
    DOCKER_GATEWAY=$(docker network inspect bridge --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)
  fi
  if [ -n "${DOCKER_GATEWAY}" ]; then
    echo "Docker gateway detected: ${DOCKER_GATEWAY}"
    export OPENAI_BASE_URL="http://${DOCKER_GATEWAY}:4000/v1"
    export OPENAI_API_KEY="sk-litellm-local"
    echo "Overridden OPENAI_BASE_URL to: ${OPENAI_BASE_URL}"
    echo "Overridden OPENAI_API_KEY to: sk-litellm-local"
  else
    echo "WARNING: Could not detect docker gateway, using original OPENAI_BASE_URL"
  fi
fi

# ══════════════════════════════════════════════════════════════════════
# Build harbor run command
# ══════════════════════════════════════════════════════════════════════
HARBOR_ARGS=()

if [ "${DATASET_TYPE}" = "registry" ]; then
  HARBOR_ARGS+=(-d "${DATASET_NAME}@${DATASET_VERSION}")
  HARBOR_ARGS+=(-t "${INSTANCE_ID}")
else
  HARBOR_ARGS+=(-p "${INSTANCE_ID}")
fi

HARBOR_ARGS+=(-a "${HARBOR_AGENT}")
HARBOR_ARGS+=(-m "${MODEL_NAME}")
HARBOR_ARGS+=(--env "${HARBOR_ENV}")
HARBOR_ARGS+=(--jobs-dir "${HARBOR_JOB_RESULT_PATH}")
HARBOR_ARGS+=(--n-attempts "${N_ATTEMPTS}")
HARBOR_ARGS+=(--n-concurrent "${N_CONCURRENT}")
HARBOR_ARGS+=(--timeout-multiplier "${TIMEOUT_MULTIPLIER}")
HARBOR_ARGS+=(--max-retries "${MAX_RETRIES}")
HARBOR_ARGS+=(--ak "max_turns=${MAX_ITERATIONS}")

# Timeout multipliers
[ -n "${AGENT_TIMEOUT_MULTIPLIER}" ]              && HARBOR_ARGS+=(--agent-timeout-multiplier "${AGENT_TIMEOUT_MULTIPLIER}")
[ -n "${VERIFIER_TIMEOUT_MULTIPLIER}" ]           && HARBOR_ARGS+=(--verifier-timeout-multiplier "${VERIFIER_TIMEOUT_MULTIPLIER}")
[ -n "${AGENT_SETUP_TIMEOUT_MULTIPLIER}" ]        && HARBOR_ARGS+=(--agent-setup-timeout-multiplier "${AGENT_SETUP_TIMEOUT_MULTIPLIER}")
[ -n "${ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER}" ]  && HARBOR_ARGS+=(--environment-build-timeout-multiplier "${ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER}")

# Absolute timeout overrides (take precedence over multipliers when both set)
[ -n "${AGENT_TIMEOUT}" ]                         && HARBOR_ARGS+=(--agent-timeout "${AGENT_TIMEOUT}")
[ -n "${AGENT_SETUP_TIMEOUT}" ]                   && HARBOR_ARGS+=(--agent-setup-timeout "${AGENT_SETUP_TIMEOUT}")
[ -n "${VERIFIER_TIMEOUT}" ]                      && HARBOR_ARGS+=(--verifier-timeout "${VERIFIER_TIMEOUT}")

# Resource overrides for task container inside DinD
[ -n "${OVERRIDE_CPUS}" ]       && HARBOR_ARGS+=(--override-cpus "${OVERRIDE_CPUS}")
[ -n "${OVERRIDE_MEMORY_MB}" ]  && HARBOR_ARGS+=(--override-memory-mb "${OVERRIDE_MEMORY_MB}")
[ -n "${OVERRIDE_STORAGE_MB}" ] && HARBOR_ARGS+=(--override-storage-mb "${OVERRIDE_STORAGE_MB}")

# Retry exception filters
if [ -n "${RETRY_INCLUDE}" ]; then
  for exc in ${RETRY_INCLUDE}; do HARBOR_ARGS+=(--retry-include "${exc}"); done
fi
if [ -n "${RETRY_EXCLUDE}" ]; then
  for exc in ${RETRY_EXCLUDE}; do HARBOR_ARGS+=(--retry-exclude "${exc}"); done
fi

[ -n "${AGENT_VERSION}" ] && HARBOR_ARGS+=(--ak "version=${AGENT_VERSION}")

# ── Terminus-2 agent kwargs ──
if [ "${HARBOR_AGENT}" = "terminus-2" ]; then
  [ -n "${TEMPERATURE}" ]                         && HARBOR_ARGS+=(--ak "temperature=${TEMPERATURE}")
  [ -n "${INTERLEAVED_THINKING}" ]                && HARBOR_ARGS+=(--ak "interleaved_thinking=${INTERLEAVED_THINKING}")
  [ -n "${THINKING_TYPE}" ]                       && HARBOR_ARGS+=(--ak "thinking_type=${THINKING_TYPE}")
  [ -n "${REASONING_EFFORT}" ]                    && HARBOR_ARGS+=(--ak "reasoning_effort=${REASONING_EFFORT}")
  [ -n "${REASONING_BUDGET_TOKENS}" ]             && HARBOR_ARGS+=(--ak "reasoning_budget_tokens=${REASONING_BUDGET_TOKENS}")
  [ -n "${MAX_TOKENS}" ]                          && HARBOR_ARGS+=(--ak "max_tokens=${MAX_TOKENS}")
  [ -n "${USE_HACK}" ]                            && HARBOR_ARGS+=(--ak "use_hack=${USE_HACK}")
  [ -n "${CONTEXT_1M}" ]                          && HARBOR_ARGS+=(--ak "context_1m=${CONTEXT_1M}")
  [ -n "${STREAM}" ]                              && HARBOR_ARGS+=(--ak "stream=${STREAM}")
  [ -n "${PARSER_NAME}" ]                         && HARBOR_ARGS+=(--ak "parser_name=${PARSER_NAME}")
  [ -n "${MAX_THINKING_TOKENS}" ]                 && HARBOR_ARGS+=(--ak "max_thinking_tokens=${MAX_THINKING_TOKENS}")
  [ -n "${ENABLE_SUMMARIZE}" ]                    && HARBOR_ARGS+=(--ak "enable_summarize=${ENABLE_SUMMARIZE}")
  [ -n "${PROACTIVE_SUMMARIZATION_THRESHOLD}" ]   && HARBOR_ARGS+=(--ak "proactive_summarization_threshold=${PROACTIVE_SUMMARIZATION_THRESHOLD}")
  [ -n "${MODEL_INFO}" ]                          && HARBOR_ARGS+=(--ak "model_info=${MODEL_INFO}")
  [ -n "${LLM_KWARGS}" ]                          && HARBOR_ARGS+=(--ak "llm_kwargs=${LLM_KWARGS}")
  [ -n "${LLM_CALL_KWARGS}" ]                     && HARBOR_ARGS+=(--ak "llm_call_kwargs=${LLM_CALL_KWARGS}")
  [ -n "${STORE_ALL_MESSAGES}" ]                  && HARBOR_ARGS+=(--ak "store_all_messages=${STORE_ALL_MESSAGES}")
  [ -n "${RECORD_TERMINAL_SESSION}" ]             && HARBOR_ARGS+=(--ak "record_terminal_session=${RECORD_TERMINAL_SESSION}")
  [ -n "${USE_RESPONSES_API}" ]                   && HARBOR_ARGS+=(--ak "use_responses_api=${USE_RESPONSES_API}")
  [ -n "${LLM_BACKEND}" ]                         && HARBOR_ARGS+=(--ak "llm_backend=${LLM_BACKEND}")
  [ -n "${COLLECT_ROLLOUT_DETAILS}" ]             && HARBOR_ARGS+=(--ak "collect_rollout_details=${COLLECT_ROLLOUT_DETAILS}")
  [ -n "${MAX_QUERY_LLM_RETRIES}" ]               && HARBOR_ARGS+=(--ak "max_query_llm_retries=${MAX_QUERY_LLM_RETRIES}")
  [ -n "${LLM_MAX_RETRIES}" ]                     && HARBOR_ARGS+=(--ak "llm_max_retries=${LLM_MAX_RETRIES}")
  [ -n "${LITELLM_TIMEOUT}" ]                     && HARBOR_ARGS+=(--ak "litellm_timeout=${LITELLM_TIMEOUT}")
  [ -n "${TRAJECTORY_CONFIG}" ]                   && HARBOR_ARGS+=(--ak "trajectory_config=${TRAJECTORY_CONFIG}")
fi

# ── Claude-code agent kwargs ──
if [ "${HARBOR_AGENT}" = "claude-code" ]; then
  [ -n "${MAX_THINKING_TOKENS}" ]                 && HARBOR_ARGS+=(--ak "max_thinking_tokens=${MAX_THINKING_TOKENS}")
  [ -n "${REASONING_EFFORT}" ]                    && HARBOR_ARGS+=(--ak "reasoning_effort=${REASONING_EFFORT}")
  [ -n "${ALLOWED_TOOLS}" ]                       && HARBOR_ARGS+=(--ak "allowed_tools=${ALLOWED_TOOLS}")
  [ -n "${DISALLOWED_TOOLS}" ]                    && HARBOR_ARGS+=(--ak "disallowed_tools=${DISALLOWED_TOOLS}")
  [ -n "${BARE}" ]                                && HARBOR_ARGS+=(--ak "bare=${BARE}")
fi

# ── OpenClaw agent kwargs ──
if [ "${HARBOR_AGENT}" = "openclaw" ]; then
  [ -n "${THINKING_LEVEL}" ]                      && HARBOR_ARGS+=(--ak "thinking_level=${THINKING_LEVEL}")
  [ -n "${MODEL_BASE_URL}" ]                      && HARBOR_ARGS+=(--ak "model_base_url=${MODEL_BASE_URL}")
  [ -n "${MODEL_API_KEY}" ]                       && HARBOR_ARGS+=(--ak "model_api_key=${MODEL_API_KEY}")
  HARBOR_ARGS+=(--ak "model_reasoning=true")
  [ -n "${MODEL_API:-}" ]                         && HARBOR_ARGS+=(--ak "model_api=${MODEL_API}")
  [ -n "${MODEL_CONTEXT_LENGTH}" ]                && HARBOR_ARGS+=(--ak "model_context_length=${MODEL_CONTEXT_LENGTH}")
  [ -n "${MODEL_MAX_TOKENS}" ]                    && HARBOR_ARGS+=(--ak "model_max_tokens=${MODEL_MAX_TOKENS}")
  [ -n "${TEMPERATURE}" ]                         && HARBOR_ARGS+=(--ak "temperature=${TEMPERATURE}")
  [ -n "${OPENCLAW_CONFIG}" ]                     && HARBOR_ARGS+=(--ak "openclaw_config=${OPENCLAW_CONFIG}")

  FORWARDED_AGENT_ENV_VARS=(
    HARBOR_NPM_INSTALL_MODE
    HARBOR_NODE_DIST_BASE_URL
    HARBOR_NPM_REGISTRY_URL
    OPENCLAW_NODE_VERSION
    HARBOR_OPENCLAW_INSTALL_VERBOSE
    OPENCLAW_NPM_LOGLEVEL
  )
  for env_name in "${FORWARDED_AGENT_ENV_VARS[@]}"; do
    env_value="${!env_name:-}"
    if [ -n "${env_value}" ]; then
      HARBOR_ARGS+=(--ae "${env_name}=${env_value}")
    fi
  done
fi

# ── Codex agent kwargs ──
# Mirrors qwen/Agent-Service/.../harbor/main.j2 L893-L912.
if [ "${HARBOR_AGENT}" = "codex" ]; then
  [ -n "${REASONING_EFFORT}" ]        && HARBOR_ARGS+=(--ak "reasoning_effort=${REASONING_EFFORT}")
  [ -n "${REASONING_SUMMARY}" ]       && HARBOR_ARGS+=(--ak "reasoning_summary=${REASONING_SUMMARY}")
  if [ "${FORCE_PROXY}" = "true" ]; then
    HARBOR_ARGS+=(--ak "normalize_model_slug=true")
  fi
  [ -n "${STREAM_MAX_RETRIES}" ]      && HARBOR_ARGS+=(--ak "stream_max_retries=${STREAM_MAX_RETRIES}")
  [ -n "${STREAM_IDLE_TIMEOUT_MS}" ]  && HARBOR_ARGS+=(--ak "stream_idle_timeout_ms=${STREAM_IDLE_TIMEOUT_MS}")
  [ -n "${REQUEST_MAX_RETRIES}" ]     && HARBOR_ARGS+=(--ak "request_max_retries=${REQUEST_MAX_RETRIES}")
fi

# Extra --ak escape hatch
if [ -n "${EXTRA_AGENT_KWARGS}" ]; then
  for kv in ${EXTRA_AGENT_KWARGS}; do HARBOR_ARGS+=(--ak "${kv}"); done
fi

# Extra --ae escape hatch: AGENT_EXTRA_ENV is a JSON dict {KEY: VALUE}
# that gets forwarded as --ae KEY=VALUE to harbor run (env vars for the
# agent process, distinct from --ak agent kwargs).
if [ -n "${AGENT_EXTRA_ENV}" ]; then
  while IFS='=' read -r ae_key ae_value; do
    [ -z "${ae_key}" ] && continue
    HARBOR_ARGS+=(--ae "${ae_key}=${ae_value}")
  done < <(python3 -c "
import json, sys
try:
    data = json.loads(sys.argv[1])
    for k, v in data.items():
        print(f'{k}={v}')
except Exception as e:
    print(f'ERROR: Failed to parse AGENT_EXTRA_ENV as JSON: {e}', file=sys.stderr)
    sys.exit(1)
" "${AGENT_EXTRA_ENV}")
fi

HARBOR_ARGS+=(--no-delete)

# Skip harbor run interactive confirmation when SKIP_CONFIRM=true (mirrors Agent-Service `skip_confirm` field)
if [ "${SKIP_CONFIRM:-}" = "true" ]; then
  HARBOR_ARGS+=(-y)
fi

echo "======= RUNNING HARBOR COMMAND ======="
echo "harbor run ${HARBOR_ARGS[*]}"
echo "======================================"

harbor run "${HARBOR_ARGS[@]}" 2>&1 | tee ${HARBOR_JOB_RESULT_PATH}/harbor_stdout.txt

if [ ${PIPESTATUS[0]} -eq 0 ]; then
  echo "Harbor execution completed successfully"
else
  echo "ERROR: Harbor execution failed"
  exit 1
fi

# ── Collect results ──
mkdir -p "${OUTPUT_DIR}"
cp -r "${HARBOR_JOB_RESULT_PATH}/." "${OUTPUT_DIR}/"

# Mask apiKey in trial.log files to avoid leaking secrets
find "${OUTPUT_DIR}" -name "trial.log" -type f 2>/dev/null | while read -r logfile; do
  sed -i 's/"apiKey": "[^"]*"/"apiKey": "sk-masked"/g' "${logfile}" 2>/dev/null || true
done

# Copy opencode.json to OUTPUT_DIR (with apiKey masked)
if [ "${HARBOR_AGENT}" = "opencode" ] && [ -f "${SHARED_DIR}/opencode.json" ]; then
  cp "${SHARED_DIR}/opencode.json" "${OUTPUT_DIR}/opencode.json"
  sed -i 's/"apiKey": "[^"]*"/"apiKey": "sk-masked"/g' "${OUTPUT_DIR}/opencode.json" 2>/dev/null || true
fi

# Copy proxy logs if proxy was used
if [ "${USE_PROXY}" = "1" ]; then
  if [ -d "${OUTPUT_DIR}/claudecode_logs" ] && [ "$(ls -A ${OUTPUT_DIR}/claudecode_logs 2>/dev/null)" ]; then
    mkdir -p ${OUTPUT_DIR}/claudecode_logs/
    # Skip copy if source and destination resolve to the same path
    _src_real=$(realpath ${OUTPUT_DIR}/claudecode_logs 2>/dev/null || echo "${OUTPUT_DIR}/claudecode_logs")
    _dst_real=$(realpath ${OUTPUT_DIR}/claudecode_logs 2>/dev/null || echo "${OUTPUT_DIR}/claudecode_logs")
    if [ "${_src_real}" != "${_dst_real}" ]; then
      cp -r ${OUTPUT_DIR}/claudecode_logs/* ${OUTPUT_DIR}/claudecode_logs/
    fi
  fi
fi

# Copy codex LiteLLM proxy logs if codex + force_proxy
if [ "${HARBOR_AGENT}" = "codex" ] && [ "${FORCE_PROXY}" = "true" ]; then
  if [ -d "${OUTPUT_DIR}/codex_litellm_logs" ] && [ "$(ls -A ${OUTPUT_DIR}/codex_litellm_logs 2>/dev/null)" ]; then
    mkdir -p ${OUTPUT_DIR}/codex_litellm_logs/
    # Skip copy if source and destination resolve to the same path
    _src_real=$(realpath ${OUTPUT_DIR}/codex_litellm_logs 2>/dev/null || echo "${OUTPUT_DIR}/codex_litellm_logs")
    _dst_real=$(realpath ${OUTPUT_DIR}/codex_litellm_logs 2>/dev/null || echo "${OUTPUT_DIR}/codex_litellm_logs")
    if [ "${_src_real}" != "${_dst_real}" ]; then
      cp -r ${OUTPUT_DIR}/codex_litellm_logs/* ${OUTPUT_DIR}/codex_litellm_logs/
    fi
  fi
fi

# ── Write harbor_summary.json ──
if [ "${DATASET_TYPE}" = "registry" ]; then
cat > ${OUTPUT_DIR}/harbor_summary.json <<EOF
{
  "agent": "${HARBOR_AGENT}",
  "dataset_type": "${DATASET_TYPE}",
  "dataset_name": "${DATASET_NAME}",
  "dataset_version": "${DATASET_VERSION}",
  "task_id": "${INSTANCE_ID}",
  "model": "${MODEL_NAME}",
  "environment": "${HARBOR_ENV}",
  "status": "completed"
}
EOF
else
cat > ${OUTPUT_DIR}/harbor_summary.json <<EOF
{
  "agent": "${HARBOR_AGENT}",
  "dataset_type": "${DATASET_TYPE}",
  "task_id": "${INSTANCE_ID}",
  "model": "${MODEL_NAME}",
  "environment": "${HARBOR_ENV}",
  "status": "completed"
}
EOF
fi

# ── Extract metrics for AP callback ──
# Mirrors SWE-Post-Process/swe_post_process/plugins/agents/harbor.py:
#   - task_score / passed     <- results.json:stats.evals.*.metrics[0].mean
#   - agent_exit_reason       <- result.json:exception_info.exception_type
#   - internal_error_type     <- result.json:agent_result.metadata.internal_error_type
#   - scaffold                <- result.json:agent_info.name
#   - small_calls / small_success / small_failed / small_unknown
#                             <- claudecode_logs/proxy_stdout.log
python3 - "$OUTPUT_DIR" "$OUTPUT_DIR/metrics.json" <<'PYEOF' || true
import json, re, sys
from pathlib import Path

OUTPUT_DIR = Path(sys.argv[1])
METRICS_OUT = sys.argv[2]

metrics = {"task_score": 0.0, "passed": False}

# 1) Find per-trial result.json (latest by mtime) — primary source for both
#    task_score and extra fields, aligned with SWE-Post-Process/skillsbench.py
trial_result = None
trial_candidates = sorted(
    [p for p in OUTPUT_DIR.glob("*/*/result.json") if p.is_file()],
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
if trial_candidates:
    trial_result = trial_candidates[0]

agent_exit_reason = None
internal_error_type = None
scaffold = None
if trial_result is not None:
    try:
        d = json.load(open(trial_result))
        # task_score: verifier_result.rewards.reward (same as SWE-Post-Process)
        vr = d.get("verifier_result")
        reward = 0.0
        if isinstance(vr, dict):
            rewards = vr.get("rewards") or {}
            try:
                reward = float(rewards.get("reward", 0.0))
            except (TypeError, ValueError):
                reward = 0.0
        # passed: reward == 1.0 AND verifier_result present AND no exception
        #   (aligned with SWE-Post-Process skillsbench.py L88)
        has_exception = d.get("exception_info") is not None
        metrics["task_score"] = reward
        metrics["passed"] = bool(reward == 1.0 and vr is not None and not has_exception)
        # extra fields
        ei = d.get("exception_info") or {}
        if isinstance(ei, dict):
            agent_exit_reason = ei.get("exception_type")
        ar = d.get("agent_result") or {}
        md = (ar.get("metadata") or {}) if isinstance(ar, dict) else {}
        if isinstance(md, dict):
            internal_error_type = md.get("internal_error_type")
        ai = d.get("agent_info") or {}
        if isinstance(ai, dict):
            scaffold = ai.get("name")
    except Exception as e:
        print(f"[metrics] failed reading trial result {trial_result}: {e}", file=sys.stderr)
else:
    # 2) Fallback: rollout-level stats.evals.*.metrics[0].mean when no trial result found
    agg_file = None
    for cand in OUTPUT_DIR.rglob("results.json"):
        agg_file = cand
        break
    if agg_file is None:
        for cand in OUTPUT_DIR.rglob("result.json"):
            try:
                j = json.load(open(cand))
                if isinstance(j, dict) and j.get("stats"):
                    agg_file = cand
                    break
            except Exception:
                continue
    if agg_file is not None:
        try:
            r = json.load(open(agg_file))
            for ev in (r.get("stats", {}) or {}).get("evals", {}).values():
                m = (ev.get("metrics") or [{}])[0]
                if "mean" in m:
                    metrics["task_score"] = m["mean"]
                    metrics["passed"] = bool(m["mean"] and m["mean"] > 0)
                    break
        except Exception as e:
            print(f"[metrics] fallback: failed reading aggregated results: {e}", file=sys.stderr)

# 3) small-model stats from claudecode_logs/proxy_stdout.log
_STRICT_SUCCESS_STEPS = {
    "stream_done_received",
    "stream_finalization_message_stop",
    "non_stream_response_received",
}
_RETRY_INTERMEDIATE_STEPS = {
    "openai_non_stream_retry",
    "openai_stream_retry",
    "native_non_stream_retry",
    "native_stream_retry",
}
_RID_RE = re.compile(r'"request_id"\s*:\s*"([^"]*)"')
_STEP_RE = re.compile(r'"step"\s*:\s*"([^"]*)"')
_CODE_RE = re.compile(r'"code"\s*:\s*"([^"]*)"')

small = {"small_calls": 0, "small_success": 0, "small_failed": 0, "small_unknown": 0}
proxy_log = OUTPUT_DIR / "claudecode_logs" / "proxy_stdout.log"
if proxy_log.exists():
    def _first(rg, line):
        m = rg.search(line)
        return m.group(1) if m else ""
    def _looks_like_error(step, code):
        if step in _RETRY_INTERMEDIATE_STEPS:
            return False
        sl = step.lower()
        if "error" in sl or "exception" in sl:
            return True
        if code:
            try:
                return int(code) >= 400
            except ValueError:
                return False
        return False
    def _load_record(line):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            idx = line.find("{")
            if idx < 0:
                return None
            try:
                return json.loads(line[idx:])
            except json.JSONDecodeError:
                return None

    calls = {}  # request_id -> [has_success, has_error]
    try:
        with proxy_log.open("r", encoding="utf-8", errors="replace") as h:
            for line in h:
                if not line.startswith("{"):
                    continue
                step = _first(_STEP_RE, line)
                if not step:
                    continue
                if step == "request_routing_selected":
                    rec = _load_record(line)
                    if not rec:
                        continue
                    rid = rec.get("request_id") or ""
                    ctx = rec.get("context") or {}
                    if not rid or ctx.get("model_tier") != "small":
                        continue
                    calls[rid] = [False, False]
                    continue
                rid = _first(_RID_RE, line)
                if not rid or rid not in calls:
                    continue
                code = _first(_CODE_RE, line)
                if step in _STRICT_SUCCESS_STEPS:
                    calls[rid][0] = True
                if _looks_like_error(step, code):
                    calls[rid][1] = True
    except Exception as e:
        print(f"[metrics] failed reading proxy log {proxy_log}: {e}", file=sys.stderr)
        calls = {}
    succ = sum(1 for s, err in calls.values() if s and not err)
    fail = sum(1 for s, err in calls.values() if err)
    total = len(calls)
    small = {
        "small_calls": total,
        "small_success": succ,
        "small_failed": fail,
        "small_unknown": total - succ - fail,
    }
# Wrap extra fields under "extra_fields" key
metrics["extra_fields"] = {
    "agent_exit_reason": agent_exit_reason,
    "internal_error_type": internal_error_type,
    "scaffold": scaffold,
    **small,
}

with open(METRICS_OUT, "w") as f:
    json.dump(metrics, f, indent=2)
print(f"[metrics] metrics.json written: {metrics}")
PYEOF
