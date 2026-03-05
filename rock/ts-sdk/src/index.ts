/**
 * ROCK TypeScript SDK
 * Main entry point
 */

import { readFileSync } from 'fs';
import { join } from 'path';

// Version - read from package.json to ensure consistency
function getVersion(): string {
  const packageJsonPath = join(__dirname, '..', 'package.json');
  const content = readFileSync(packageJsonPath, 'utf-8');
  return JSON.parse(content).version;
}

export const VERSION: string = getVersion();

// Types
export * from './types/index.js';

// Common - explicit exports to avoid conflicts
export {
  RockException,
  InvalidParameterRockException,
  BadRequestRockError,
  InternalServerRockError,
  CommandRockError,
  raiseForCode,
  fromRockException,
} from './common/index.js';
export { RunMode, RunModeType, PID_PREFIX, PID_SUFFIX } from './common/constants.js';

// Utils - explicit exports to avoid conflicts
export { HttpUtils } from './utils/http.js';
export { retryAsync, sleep, withRetry } from './utils/retry.js';
export { deprecated, deprecatedClass } from './utils/deprecated.js';
export { isNode, getEnv, getRequiredEnv, isEnvSet } from './utils/system.js';

// EnvHub
export * from './envhub/index.js';

// Envs
export * from './envs/index.js';

// Sandbox - selective exports
export { Sandbox, SandboxGroup } from './sandbox/client.js';
export type { RunModeType as SandboxRunModeType } from './common/constants.js';
export {
  SandboxConfigSchema,
  SandboxGroupConfigSchema,
  createSandboxConfig,
  createSandboxGroupConfig,
} from './sandbox/config.js';
export type { SandboxConfig, SandboxGroupConfig, BaseConfig } from './sandbox/config.js';
export { Deploy } from './sandbox/deploy.js';
export { LinuxFileSystem } from './sandbox/file_system.js';
export { Network, SpeedupType } from './sandbox/network.js';
export { Process } from './sandbox/process.js';
export { LinuxRemoteUser } from './sandbox/remote_user.js';
export { withTimeLogging, arunWithRetry, extractNohupPid as extractNohupPidFromSandbox } from './sandbox/utils.js';

// Model
export * from './model/index.js';

// RuntimeEnv
export * from './sandbox/runtime_env/index.js';

// ModelService (sandbox)
export * from './sandbox/model_service/index.js';

// Agent
export * from './sandbox/agent/index.js';
