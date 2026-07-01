/**
 * Config hierarchy for the Job system.
 *
 * JobConfig    — base config with shared job-scheduling fields
 * BashJobConfig — simple script execution
 *
 * Environment config lives in envhub/schema.ts.
 * Harbor's HarborJobConfig lives in bench/models/job/config.ts.
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// Helper: generate timestamp-based default job_name
// ---------------------------------------------------------------------------

function _generateTimestampName(): string {
  const now = new Date();
  const Y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  const d = String(now.getDate()).padStart(2, '0');
  const H = String(now.getHours()).padStart(2, '0');
  const M = String(now.getMinutes()).padStart(2, '0');
  const S = String(now.getSeconds()).padStart(2, '0');
  return `${Y}-${m}-${d}__${H}-${M}-${S}`;
}

// ---------------------------------------------------------------------------
// Internal: sync experiment_id helpers
// ---------------------------------------------------------------------------

/**
 * Returns the raw object shape for JobConfig common fields —
 * without the environment field. Callers extend from this.
 */
export function _jobConfigBaseFields() {
  return {
    job_name: z.string().nullable().default(null),
    namespace: z.string().nullable().default(null),
    experiment_id: z.string().nullable().default(null),
    labels: z.record(z.string()).default({}),
    timeout: z.number().int().default(7200),
  };
}

/**
 * Apply experiment_id sync logic after parsing.
 *
 * If JobConfig.experiment_id is set and differs from environment's
 * experimentId/experiment_id, the JobConfig value wins (silently, matching Python).
 */
export function _syncExperimentId(data: Record<string, unknown>): void {
  const expId = data['experiment_id'] as string | null;
  if (expId === null) return;

  const env = data['environment'] as Record<string, unknown> | undefined;
  if (!env) return;

  const envExpId = env['experimentId'] ?? env['experiment_id'];
  if (envExpId !== undefined && envExpId !== null && envExpId !== expId) {
    // Conflict: JobConfig.experiment_id wins (Python logs a warning here).
  }

  // Sync to environment — set camelCase key (matches TS SandboxConfig convention)
  env['experimentId'] = expId;
}

// ---------------------------------------------------------------------------
// JobConfig (base)
// ---------------------------------------------------------------------------

/**
 * Create a base JobConfig schema parameterized by the environment type.
 *
 * The environment field defaults to an empty object if omitted, which is then
 * parsed by the provided environmentSchema using its own defaults.
 * This matches Python's ``environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)``.
 *
 * Usage:
 *   const schema = JobConfigSchema(EnvironmentConfigSchema);
 *   const config = schema.parse({ job_name: 'my-job' });
 */
export function JobConfigSchema<TEnv extends z.ZodTypeAny>(environmentSchema: TEnv) {
  return z
    .object({
      environment: environmentSchema.default({}),
      ..._jobConfigBaseFields(),
    })
    .transform((data) => {
      _syncExperimentId(data as Record<string, unknown>);
      return data;
    });
}

/** Inferred type for the base JobConfig. */
export type JobConfig = {
  environment: Record<string, unknown>;
  job_name: string | null;
  namespace: string | null;
  experiment_id: string | null;
  labels: Record<string, string>;
  timeout: number;
};

/**
 * Create a JobConfig with defaults applied.
 */
export function createJobConfig<TEnv extends z.ZodTypeAny>(
  config: Partial<JobConfig> & { environment?: z.infer<TEnv> },
  environmentSchema: TEnv
): z.infer<ReturnType<typeof JobConfigSchema<TEnv>>> {
  return JobConfigSchema(environmentSchema).parse(config ?? {});
}

// ---------------------------------------------------------------------------
// BashJobConfig
// ---------------------------------------------------------------------------

/**
 * Create a BashJobConfig schema parameterized by the environment type.
 *
 * Extends base JobConfig fields and adds script/script_path.
 * Uses .strict() to forbid extra fields (matching Python ConfigDict(extra="forbid")).
 * job_name defaults to a timestamp instead of null.
 */
export function BashJobConfigSchema<TEnv extends z.ZodTypeAny>(environmentSchema: TEnv) {
  return z
    .object({
      environment: environmentSchema.default({}),
      ..._jobConfigBaseFields(),
      // Override job_name with timestamp default (different from base JobConfig)
      job_name: z.string().default(_generateTimestampName),
      script: z.string().nullable().default(null),
      script_path: z.string().nullable().default(null),
    })
    .strict()
    .transform((data) => {
      _syncExperimentId(data as Record<string, unknown>);
      return data;
    });
}

export type BashJobConfig = z.infer<ReturnType<typeof BashJobConfigSchema<z.ZodTypeAny>>>;

/**
 * Create a BashJobConfig with defaults applied.
 */
export function createBashJobConfig<TEnv extends z.ZodTypeAny>(
  config: Partial<BashJobConfig> & { environment?: z.infer<TEnv> },
  environmentSchema: TEnv
): BashJobConfig {
  return BashJobConfigSchema(environmentSchema).parse(config ?? {});
}
