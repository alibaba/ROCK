/**
 * ROCK TypeScript SDK
 * Main entry point
 */

// Version
export const VERSION = '1.2.1';

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
export { Constants, RunMode, RunModeType, PID_PREFIX, PID_SUFFIX } from './common/constants.js';

// Utils - explicit exports to avoid conflicts
export { HttpUtils } from './utils/http.js';
export { retryAsync, sleep, withRetry } from './utils/retry.js';
export { deprecated, deprecatedClass } from './utils/deprecated.js';
export { isNode, isBrowser, getEnv, getRequiredEnv, isEnvSet } from './utils/system.js';

// EnvHub
export * from './envhub/index.js';

// Envs
export * from './envs/index.js';

// Sandbox - selective exports
export { Sandbox, SandboxGroup } from './sandbox/client.js';
export type { RunModeType as SandboxRunModeType } from './sandbox/types.js';
export {
  SandboxConfigSchema,
  SandboxGroupConfigSchema,
  createSandboxConfig,
  createSandboxGroupConfig,
} from './sandbox/config.js';
export type { SandboxConfig, SandboxGroupConfig, BaseConfig } from './sandbox/config.js';
export { Deploy } from './sandbox/deploy.js';
export { LinuxFileSystem } from './sandbox/file_system.js';
export { Network } from './sandbox/network.js';
export { Process } from './sandbox/process.js';
export { LinuxRemoteUser } from './sandbox/remote_user.js';
export { withTimeLogging, arunWithRetry, extractNohupPid as extractNohupPidFromSandbox } from './sandbox/utils.js';
export { SpeedupType } from './sandbox/types.js';

// Model
export * from './model/index.js';
