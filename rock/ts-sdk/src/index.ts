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

// Model — explicit re-exports to avoid conflicts with sandbox/runtime_env (SandboxLike)
// and sandbox/model_service (ModelService, ModelServiceConfig, ModelServiceConfigSchema).
export {
  // Model client
  ModelClient,
  type ModelClientConfig,
  type PollOptions,
  // Server config
  POLLING_INTERVAL_SECONDS,
  REQUEST_TIMEOUT,
  REQUEST_START_MARKER,
  REQUEST_END_MARKER,
  RESPONSE_START_MARKER,
  RESPONSE_END_MARKER,
  SESSION_END_MARKER,
  createModelServiceConfig,
  // Trajectory
  TrajectoryRecorder,
  SequentialCursor,
  TrajectoryExhausted,
  type TrajectoryRecordParams,
  // SSE
  parseSseDataChunks,
  completionToChunkDict,
  encodeSseEvent,
  SSE_DONE,
  // Utils
  writeTraj,
  MODEL_SERVICE_REQUEST_RT,
  MODEL_SERVICE_REQUEST_COUNT,
} from './model/index.js';
// ModelService (from model/ — aliased to avoid conflict with sandbox/model_service)
export { ModelService as ServerModelService } from './model/index.js';
export type { ModelServiceConfig as ServerModelServiceConfig } from './model/index.js';
export { ModelServiceConfigSchema as ServerModelServiceConfigSchema } from './model/index.js';

// RuntimeEnv
export * from './sandbox/runtime_env/index.js';

// ModelService (sandbox)
export * from './sandbox/model_service/index.js';

// Agent
export * from './sandbox/agent/index.js';

// Bench — selective re-exports to avoid conflicts with datasets/agent models that share
// names (LocalDatasetConfig, OssRegistryInfo, RegistryDatasetConfig, AgentConfig).
// Consumers should import from 'rl-rock/bench' directly for full access.
export {
  // Constants
  DEFAULT_WAIT_TIMEOUT as BenchDefaultWaitTimeout,
  CHECK_INTERVAL as BenchCheckInterval,
  USER_DEFINED_LOGS as BenchUserDefinedLogs,
  // Enums
  EnvironmentType,
  EnvironmentTypeSchema,
  OrchestratorType,
  OrchestratorTypeSchema,
  MetricType,
  MetricTypeSchema,
  // Job config
  RetryConfigSchema,
  createRetryConfig,
  createOrchestratorConfig,
  HFRegistryInfoSchema,
  LocalRegistryInfoSchema,
  RemoteRegistryInfoSchema,
  parseDatasetConfig,
  DatasetConfigSchema,
  HarborJobConfigSchema,
  createHarborJobConfig,
  // Trial config
  createAgentConfig,
  createEnvironmentConfig,
  createVerifierConfig,
  createTaskConfig,
  createArtifactConfig,
  TemplateConfigSchema,
  NativeConfigSchema,
  RockEnvironmentConfigSchema,
  toHarborEnvironment,
  // Trial result
  ModelInfoSchema,
  AgentInfoSchema,
  AgentResultSchema,
  VerifierResultSchema,
  TimingInfoSchema,
  ExceptionInfoSchema,
  createHarborTrialResultFromJson,
  // Metric
  MetricConfigSchema,
  createMetricConfig,
} from './bench/index.js';

export type {
  // Enums
  EnvironmentType as EnvironmentTypeType,
  OrchestratorType as OrchestratorTypeType,
  MetricType as MetricTypeType,
  // Job config
  RetryConfig,
  OrchestratorConfig,
  OssRegistryInfo as BenchOssRegistryInfo,
  RemoteRegistryInfo,
  HFRegistryInfo,
  LocalRegistryInfo,
  DatasetConfig as BenchDatasetConfig,
  HarborJobConfig,
  // Trial config
  AgentConfig as BenchAgentConfig,
  EnvironmentConfig as BenchEnvironmentConfig,
  TemplateConfig,
  NativeConfig,
  VerifierConfig,
  TaskConfig,
  ArtifactConfig,
  RockEnvironmentConfig,
  // Trial result
  ModelInfo,
  AgentInfo,
  AgentResult,
  VerifierResult,
  TimingInfo,
  ExceptionInfo,
  HarborTrialResult,
  // Metric
  MetricConfig,
} from './bench/index.js';

// ── Job / Trial System ─────────────────────────────────────────────
export {
  // Result models
  JobStatus,
  TrialResultSchema,
  JobResultSchema,
  // Config models
  JobConfigSchema,
  BashJobConfigSchema,
  createJobConfig,
  createBashJobConfig,
  ComposeJobConfigSchema,
  ResourceConfigSchema,
  VolumeMountSchema,
  VolumeConfigSchema,
  ServiceConfigSchema,
  InitContainerConfigSchema,
  OSSArtifactConfigSchema,
  // Operator
  ScatterOperator,
  // Executor
  JobExecutor,
  // Job facade
  Job,
  // Trial
  AbstractTrial,
  registerTrial,
  createTrial,
  BashTrial,
  HarborTrial,
  ComposeTrial,
  // Compose utilities
  calcComposeSandboxResources,
  coerceCpu,
  coerceMemoryBytes,
  buildRunnerScript,
  buildComposeYaml,
} from './job/index.js';

export type {
  // Result models
  ExceptionInfo as JobExceptionInfo,
  TrialResult as JobTrialResult,
  JobResult as JobJobResult,
  // Config models
  JobConfig as JobBaseConfig,
  BashJobConfig as JobBashJobConfig,
  ResourceConfig as JobResourceConfig,
  VolumeMount as JobVolumeMount,
  VolumeConfig as JobVolumeConfig,
  ServiceConfig as JobServiceConfig,
  InitContainerConfig as JobInitContainerConfig,
  OSSArtifactConfig as JobOSSArtifactConfig,
  ComposeJobConfig as JobComposeJobConfig,
  // Operator
  Operator as JobOperator,
  // Executor
  JobClient,
  TrialClient,
} from './job/index.js';
