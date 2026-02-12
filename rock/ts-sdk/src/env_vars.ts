/**
 * Environment variables configuration
 */

import { getEnv, isEnvSet } from './utils/system.js';
import { homedir } from 'os';
import { join } from 'path';

/**
 * Environment variable definitions
 */
export const envVars = {
  // Logging
  get ROCK_LOGGING_PATH(): string | undefined {
    return getEnv('ROCK_LOGGING_PATH');
  },

  get ROCK_LOGGING_FILE_NAME(): string {
    return getEnv('ROCK_LOGGING_FILE_NAME', 'rocklet.log')!;
  },

  get ROCK_LOGGING_LEVEL(): string {
    return getEnv('ROCK_LOGGING_LEVEL', 'INFO')!;
  },

  // Service
  get ROCK_SERVICE_STATUS_DIR(): string {
    return getEnv('ROCK_SERVICE_STATUS_DIR', '/data/service_status')!;
  },

  get ROCK_SCHEDULER_STATUS_DIR(): string {
    return getEnv('ROCK_SCHEDULER_STATUS_DIR', '/data/scheduler_status')!;
  },

  // Config
  get ROCK_CONFIG(): string | undefined {
    return getEnv('ROCK_CONFIG');
  },

  get ROCK_CONFIG_DIR_NAME(): string {
    return getEnv('ROCK_CONFIG_DIR_NAME', 'rock-conf')!;
  },

  // Base URLs
  get ROCK_BASE_URL(): string {
    return getEnv('ROCK_BASE_URL', 'http://localhost:8080')!;
  },

  get ROCK_WORKER_ROCKLET_PORT(): number | undefined {
    const val = getEnv('ROCK_WORKER_ROCKLET_PORT');
    return val ? parseInt(val, 10) : undefined;
  },

  get ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS(): number {
    return parseInt(getEnv('ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS', '180')!, 10);
  },

  get ROCK_CODE_SANDBOX_BASE_URL(): string {
    return getEnv('ROCK_CODE_SANDBOX_BASE_URL', '')!;
  },

  // EnvHub
  get ROCK_ENVHUB_BASE_URL(): string {
    return getEnv('ROCK_ENVHUB_BASE_URL', 'http://localhost:8081')!;
  },

  get ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE(): string {
    return getEnv('ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE', 'python:3.11')!;
  },

  get ROCK_ENVHUB_DB_URL(): string {
    return getEnv(
      'ROCK_ENVHUB_DB_URL',
      `sqlite:///${join(homedir(), '.rock', 'rock_envs.db')}`
    )!;
  },

  // Auto clear
  get ROCK_DEFAULT_AUTO_CLEAR_TIME_MINUTES(): number {
    return parseInt(getEnv('ROCK_DEFAULT_AUTO_CLEAR_TIME_MINUTES', '360')!, 10);
  },

  // Ray
  get ROCK_RAY_NAMESPACE(): string {
    return getEnv('ROCK_RAY_NAMESPACE', 'xrl-sandbox')!;
  },

  get ROCK_SANDBOX_EXPIRE_TIME_KEY(): string {
    return getEnv('ROCK_SANDBOX_EXPIRE_TIME_KEY', 'expire_time')!;
  },

  get ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY(): string {
    return getEnv('ROCK_SANDBOX_AUTO_CLEAR_TIME_KEY', 'auto_clear_time')!;
  },

  // Timezone
  get ROCK_TIME_ZONE(): string {
    return getEnv('ROCK_TIME_ZONE', 'Asia/Shanghai')!;
  },

  // OSS
  get ROCK_OSS_ENABLE(): boolean {
    return getEnv('ROCK_OSS_ENABLE', 'false')?.toLowerCase() === 'true';
  },

  get ROCK_OSS_BUCKET_ENDPOINT(): string | undefined {
    return getEnv('ROCK_OSS_BUCKET_ENDPOINT');
  },

  get ROCK_OSS_BUCKET_NAME(): string | undefined {
    return getEnv('ROCK_OSS_BUCKET_NAME');
  },

  get ROCK_OSS_BUCKET_REGION(): string | undefined {
    return getEnv('ROCK_OSS_BUCKET_REGION');
  },

  // Pip
  get ROCK_PIP_INDEX_URL(): string {
    return getEnv('ROCK_PIP_INDEX_URL', 'https://mirrors.aliyun.com/pypi/simple/')!;
  },

  // Monitor
  get ROCK_MONITOR_ENABLE(): boolean {
    return getEnv('ROCK_MONITOR_ENABLE', 'false')?.toLowerCase() === 'true';
  },

  // Project
  get ROCK_PROJECT_ROOT(): string {
    return getEnv('ROCK_PROJECT_ROOT', process.cwd())!;
  },

  get ROCK_WORKER_ENV_TYPE(): string {
    return getEnv('ROCK_WORKER_ENV_TYPE', 'local')!;
  },

  get ROCK_PYTHON_ENV_PATH(): string {
    return getEnv('ROCK_PYTHON_ENV_PATH', process.cwd())!;
  },

  // Admin
  get ROCK_ADMIN_ENV(): string {
    return getEnv('ROCK_ADMIN_ENV', 'dev')!;
  },

  get ROCK_ADMIN_ROLE(): string {
    return getEnv('ROCK_ADMIN_ROLE', 'write')!;
  },

  // CLI
  get ROCK_CLI_LOAD_PATHS(): string {
    return getEnv('ROCK_CLI_LOAD_PATHS', '')!;
  },

  get ROCK_CLI_DEFAULT_CONFIG_PATH(): string {
    return getEnv('ROCK_CLI_DEFAULT_CONFIG_PATH', join(homedir(), '.rock', 'config.ini'))!;
  },

  // Model Service
  get ROCK_MODEL_SERVICE_DATA_DIR(): string {
    return getEnv('ROCK_MODEL_SERVICE_DATA_DIR', '/data/logs')!;
  },

  get ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE(): boolean {
    return getEnv('ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE', 'false')?.toLowerCase() === 'true';
  },

  // RuntimeEnv
  get ROCK_RTENV_PYTHON_V31114_INSTALL_CMD(): string {
    return getEnv(
      'ROCK_RTENV_PYTHON_V31114_INSTALL_CMD',
      '[ -f cpython31114.tar.gz ] && rm cpython31114.tar.gz; [ -d python ] && rm -rf python; wget -q -O cpython31114.tar.gz https://github.com/astral-sh/python-build-standalone/releases/download/20251120/cpython-3.11.14+20251120-x86_64-unknown-linux-gnu-install_only.tar.gz && tar -xzf cpython31114.tar.gz && mv python runtime-env'
    )!;
  },

  get ROCK_RTENV_PYTHON_V31212_INSTALL_CMD(): string {
    return getEnv(
      'ROCK_RTENV_PYTHON_V31212_INSTALL_CMD',
      '[ -f cpython-3.12.12.tar.gz ] && rm cpython-3.12.12.tar.gz; [ -d python ] && rm -rf python; wget -q -O cpython-3.12.12.tar.gz https://github.com/astral-sh/python-build-standalone/releases/20251217/cpython-3.12.12+20251217-x86_64-unknown-linux-gnu-install_only.tar.gz && tar -xzf cpython-3.12.12.tar.gz && mv python runtime-env'
    )!;
  },

  get ROCK_RTENV_NODE_V22180_INSTALL_CMD(): string {
    return getEnv(
      'ROCK_RTENV_NODE_V22180_INSTALL_CMD',
      '[ -f node.tar.xz ] && rm node.tar.xz; [ -d node ] && rm -rf node; wget -q -O node.tar.xz --tries=10 --waitretry=2 https://nodejs.org/dist/v22.18.0/node-v22.18.0-linux-x64.tar.xz && tar -xf node.tar.xz && mv node-v22.18.0-linux-x64 runtime-env'
    )!;
  },

  // Agent
  get ROCK_AGENT_PRE_INIT_BASH_CMD_LIST(): Array<{ command: string; timeoutSeconds: number }> {
    const val = getEnv('ROCK_AGENT_PRE_INIT_BASH_CMD_LIST', '[]');
    try {
      return JSON.parse(val!);
    } catch {
      return [];
    }
  },

  get ROCK_AGENT_IFLOW_CLI_INSTALL_CMD(): string {
    return getEnv('ROCK_AGENT_IFLOW_CLI_INSTALL_CMD', 'npm i -g @iflow-ai/iflow-cli@latest')!;
  },

  get ROCK_MODEL_SERVICE_INSTALL_CMD(): string {
    return getEnv('ROCK_MODEL_SERVICE_INSTALL_CMD', 'pip install rl_rock[model-service]')!;
  },

  // Doccuum
  get ROCK_DOCUUM_INSTALL_URL(): string {
    return getEnv(
      'ROCK_DOCUUM_INSTALL_URL',
      'https://raw.githubusercontent.com/stepchowfun/docuum/main/install.sh'
    )!;
  },
};

/**
 * Check if an environment variable is explicitly set
 */
export function isSet(name: string): boolean {
  return isEnvSet(name);
}
