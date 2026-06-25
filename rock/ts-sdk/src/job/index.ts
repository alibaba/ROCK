/**
 * Job module — complete Job/Trial system.
 *
 * Matches Python rock.sdk.job.__init__ exports.
 */

// Result models
export { JobStatus, ExceptionInfoSchema, TrialResultSchema, JobResultSchema } from './result.js';
export type { ExceptionInfo, TrialResult, JobResult } from './result.js';

// Config models
export { JobConfigSchema, BashJobConfigSchema, createJobConfig, createBashJobConfig, _jobConfigBaseFields, _syncExperimentId } from './config.js';
export type { JobConfig, BashJobConfig } from './config.js';

// Compose config models
export {
  ResourceConfigSchema,
  VolumeMountSchema,
  VolumeConfigSchema,
  ServiceConfigSchema,
  InitContainerConfigSchema,
  OSSArtifactConfigSchema,
  ComposeJobConfigSchema,
} from './config_compose.js';
export type {
  ResourceConfig,
  VolumeMount,
  VolumeConfig,
  ServiceConfig,
  InitContainerConfig,
  OSSArtifactConfig,
  ComposeJobConfig,
} from './config_compose.js';

// Operator
export { ScatterOperator } from './operator.js';
export type { Operator } from './operator.js';

// Executor
export { JobExecutor } from './executor.js';
export type { JobClient, TrialClient } from './executor.js';

// Job facade
export { Job } from './api.js';

// Trial abstractions and implementations
export {
  AbstractTrial,
  registerTrial,
  createTrial,
  _assignRegistryKey,
  BashTrial,
  BASH_JOB_CONFIG_KEY,
  HarborTrial,
  HARBOR_JOB_CONFIG_KEY,
  ComposeTrial,
  COMPOSE_JOB_CONFIG_KEY,
} from './trial/index.js';
export type { ISandbox } from './trial/index.js';

// Compose utilities
export {
  calcComposeSandboxResources,
  coerceCpu,
  coerceMemoryBytes,
  buildRunnerScript,
  buildComposeYaml,
} from './compose/index.js';
