/**
 * Job configuration models — aligned with rock.sdk.bench.models.job.config
 *
 * HarborJobConfig extends the base JobConfig (fields inlined for now; will be
 * extracted to src/job/config.ts when Task #7 builds the Job module).
 */

import { z } from 'zod';
import { OrchestratorType, OrchestratorTypeSchema } from '../orchestrator_type.js';
import { MetricConfigSchema } from '../metric/config.js';
import {
  AgentConfigSchema,
  RockEnvironmentConfigSchema,
  VerifierConfigSchema,
  ArtifactConfigSchema,
  TaskConfigSchema,
} from '../trial/config.js';

// ---------------------------------------------------------------------------
// RetryConfig
// ---------------------------------------------------------------------------

/** Default exception types excluded from retry (matching Python defaults). */
const DEFAULT_EXCLUDE_EXCEPTIONS = [
  'AgentTimeoutError',
  'VerifierTimeoutError',
  'RewardFileNotFoundError',
  'RewardFileEmptyError',
  'VerifierOutputParseError',
];

export const RetryConfigSchema = z.object({
  max_retries: z.number().int().min(0).default(0),
  include_exceptions: z.array(z.string()).nullable().default(null),
  exclude_exceptions: z.array(z.string()).default(DEFAULT_EXCLUDE_EXCEPTIONS),
  wait_multiplier: z.number().default(1.0),
  min_wait_sec: z.number().default(1.0),
  max_wait_sec: z.number().default(60.0),
});

export type RetryConfig = z.infer<typeof RetryConfigSchema>;

export function createRetryConfig(config?: Partial<RetryConfig>): RetryConfig {
  return RetryConfigSchema.parse(config ?? {});
}

// ---------------------------------------------------------------------------
// OrchestratorConfig
// ---------------------------------------------------------------------------

export const OrchestratorConfigSchema = z.object({
  type: OrchestratorTypeSchema.default(OrchestratorType.LOCAL),
  n_concurrent_trials: z.number().int().default(4),
  quiet: z.boolean().default(false),
  retry: RetryConfigSchema.default({}),
  kwargs: z.record(z.unknown()).default({}),
});

export type OrchestratorConfig = z.infer<typeof OrchestratorConfigSchema>;

export function createOrchestratorConfig(config?: Partial<OrchestratorConfig>): OrchestratorConfig {
  return OrchestratorConfigSchema.parse(config ?? {});
}

// ---------------------------------------------------------------------------
// Registry info models
// ---------------------------------------------------------------------------

export const OssRegistryInfoSchema = z.object({
  split: z.string().nullable().default(null),
  revision: z.string().nullable().default(null),
  oss_dataset_path: z.string().nullable().default(null),
  oss_access_key_id: z.string().nullable().default(null),
  oss_access_key_secret: z.string().nullable().default(null),
  oss_region: z.string().nullable().default(null),
  oss_endpoint: z.string().nullable().default(null),
  oss_bucket: z.string().nullable().default(null),
});

export type OssRegistryInfo = z.infer<typeof OssRegistryInfoSchema>;

export const RemoteRegistryInfoSchema = z.object({
  name: z.string().nullable().default(null),
  url: z
    .string()
    .default('https://raw.githubusercontent.com/laude-institute/harbor/main/registry.json'),
});

export type RemoteRegistryInfo = z.infer<typeof RemoteRegistryInfoSchema>;

export const HFRegistryInfoSchema = z.object({
  split: z.string().nullable().default(null),
  revision: z.string().nullable().default(null),
});

export type HFRegistryInfo = z.infer<typeof HFRegistryInfoSchema>;

export const LocalRegistryInfoSchema = z.object({
  name: z.string().nullable().default(null),
  path: z.string().min(1, 'path is required'),
});

export type LocalRegistryInfo = z.infer<typeof LocalRegistryInfoSchema>;

/** Union of all supported registry types (not discriminated — matches Python union). */
export const RegistryUnionSchema = z.union([
  OssRegistryInfoSchema,
  RemoteRegistryInfoSchema,
  LocalRegistryInfoSchema,
  HFRegistryInfoSchema,
]);

export type RegistryInfo = z.infer<typeof RegistryUnionSchema>;

// ---------------------------------------------------------------------------
// Dataset configs (discriminated union: local | registry)
// ---------------------------------------------------------------------------

/** Common fields shared by all dataset config types. */
const BaseDatasetFields = {
  task_names: z.array(z.string()).nullable().default(null),
  exclude_task_names: z.array(z.string()).nullable().default(null),
  n_tasks: z.number().int().nullable().default(null),
} as const;

export const LocalDatasetConfigSchema = z.object({
  ...BaseDatasetFields,
  kind: z.literal('local'),
  path: z.string().min(1, 'path is required'),
});

export type LocalDatasetConfig = z.infer<typeof LocalDatasetConfigSchema>;

/** Registry dataset — version is auto-inferred from OSS registry.split via transform. */
export const RegistryDatasetConfigSchema = z.object({
  ...BaseDatasetFields,
  kind: z.literal('registry'),
  registry: RegistryUnionSchema,
  name: z.string().min(1, 'name is required'),
  version: z.string().nullable().default(null),
  overwrite: z.boolean().default(false),
  download_dir: z.string().nullable().default(null),
});

export type RegistryDatasetConfig = z.infer<typeof RegistryDatasetConfigSchema>;

/**
 * Inferred type from discriminated union (post-transform).
 * We use the pre-transform schemas for the union and apply version inference
 * separately for those who need it.
 */
export const DatasetConfigSchema = z.discriminatedUnion('kind', [
  LocalDatasetConfigSchema,
  RegistryDatasetConfigSchema,
]);

export type DatasetConfig = z.infer<typeof DatasetConfigSchema>;

/**
 * Parse a DatasetConfig with version inference applied (mirrors Python
 * _infer_version_from_split validator on RegistryDatasetConfig).
 */
export function parseDatasetConfig(data: unknown): DatasetConfig {
  const parsed = DatasetConfigSchema.parse(data);
  if (parsed.kind === 'registry' && parsed.version === null) {
    const reg = parsed.registry as OssRegistryInfo;
    if ('split' in reg && reg.split) {
      parsed.version = reg.revision
        ? `${reg.split}@${reg.revision}`
        : reg.split;
    }
  }
  return parsed;
}

// ---------------------------------------------------------------------------
// HarborJobConfig
//
// Fields inline from base JobConfig (job_name, namespace, experiment_id,
// labels, timeout). TODO: extract base JobConfigSchema to src/job/config.ts
// when Task #7 (Job/Trial system) is implemented.
// ---------------------------------------------------------------------------

const BASE_TIMEOUT_DEFAULT = 7200;
const DEFAULT_WAIT_TIMEOUT_FALLBACK = 7200;

// Schema for (string | ArtifactConfig) — a union type used in the artifacts array
const ArtifactOrStringSchema = z.union([
  z.string(),
  ArtifactConfigSchema,
]);

export const HarborJobConfigSchema = z
  .object({
    // ---- base JobConfig fields (inlined) ----
    environment: RockEnvironmentConfigSchema.default({}),
    job_name: z.string().nullable().default(null),
    namespace: z.string().nullable().default(null),
    experiment_id: z.string().min(1, 'experiment_id is required'),
    labels: z.record(z.string()).default({}),
    timeout: z.number().int().default(BASE_TIMEOUT_DEFAULT),

    // ---- Harbor-native fields ----
    jobs_dir: z.string().default('/data/logs/user-defined/jobs'),
    n_attempts: z.number().int().default(1),
    timeout_multiplier: z.number().default(1.0),
    agent_timeout_multiplier: z.number().nullable().default(null),
    verifier_timeout_multiplier: z.number().nullable().default(null),
    agent_setup_timeout_multiplier: z.number().nullable().default(null),
    environment_build_timeout_multiplier: z.number().nullable().default(null),
    debug: z.boolean().default(false),
    orchestrator: OrchestratorConfigSchema.default({}),
    verifier: VerifierConfigSchema.default({}),
    metrics: z.array(MetricConfigSchema).default([]),
    agents: z.array(AgentConfigSchema).default([{}]),
    datasets: z.array(DatasetConfigSchema).default([]),
    tasks: z.array(TaskConfigSchema).default([]),
    artifacts: z.array(ArtifactOrStringSchema).default([]),
  })
  .superRefine((data, ctx) => {
    // ---- Validator: _sync_experiment_id ----
    const envExp = data.environment.experiment_id;
    if (envExp !== null && envExp !== data.experiment_id) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `experiment_id mismatch: JobConfig has '${data.experiment_id}', but environment (SandboxConfig) has '${envExp}'`,
        path: ['environment', 'experiment_id'],
      });
    }
    // Sync experiment_id to environment
    if (data.environment.experiment_id === null) {
      data.environment.experiment_id = data.experiment_id;
    }

    // ---- Validator: _auto_job_name ----
    if (data.job_name !== null) {
      if (data.job_name.includes('/')) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "job_name must not contain '/'",
          path: ['job_name'],
        });
      }
    } else {
      // Auto-generate: {dataset_name}_{task_name if single}_{uuid8}
      const parts: string[] = [];
      if (data.datasets.length > 0) {
        const ds = data.datasets[0]!;
        if (ds.kind === 'registry' && ds.name) {
          parts.push(ds.name.split('/').pop() ?? ds.name);
        }
        const taskNames = ds.task_names ?? [];
        if (taskNames.length === 1 && taskNames[0]) {
          parts.push(taskNames[0].split('/').pop() ?? taskNames[0]);
        }
      }
      // Generate 8-char hex UUID
      const uuid8 = Array.from({ length: 8 }, () =>
        Math.floor(Math.random() * 16).toString(16)
      ).join('');
      parts.push(uuid8);
      data.job_name = parts.join('_');
    }

    // ---- Validator: _compute_effective_timeout ----
    if (data.timeout === BASE_TIMEOUT_DEFAULT) {
      const multiplier = data.timeout_multiplier || 1.0;
      let agentTimeout: number | null = null;
      if (data.agents.length > 0) {
        const a = data.agents[0]!;
        agentTimeout = a.max_timeout_sec ?? a.override_timeout_sec ?? null;
      }
      if (agentTimeout !== null) {
        data.timeout = Math.floor(agentTimeout * multiplier) + 600;
      } else {
        data.timeout = Math.floor(DEFAULT_WAIT_TIMEOUT_FALLBACK * multiplier);
      }
    }
  })
  .transform((data) => {
    // Post-validation: sync namespace to oss_mirror
    if (data.namespace !== null && data.environment.oss_mirror !== null) {
      (data.environment.oss_mirror as Record<string, unknown>).namespace = data.namespace;
    }
    // Sync experiment_id to oss_mirror
    if (data.environment.oss_mirror !== null) {
      (data.environment.oss_mirror as Record<string, unknown>).experiment_id = data.experiment_id;
    }
    return data;
  });

export type HarborJobConfig = z.infer<typeof HarborJobConfigSchema>;

/**
 * Create a HarborJobConfig with defaults applied.
 *
 * experiment_id is required. All other fields are optional and will be filled
 * with defaults (including auto-generated job_name).
 */
export function createHarborJobConfig(
  config: Pick<HarborJobConfig, 'experiment_id'> & Partial<HarborJobConfig>
): HarborJobConfig {
  return HarborJobConfigSchema.parse(config);
}
