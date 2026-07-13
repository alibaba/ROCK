/**
 * Sandbox configuration
 */

import { z } from 'zod';
import { envVars } from '../env_vars.js';

/**
 * Base configuration schema
 */
export const BaseConfigSchema = z.object({
  baseUrl: z.string().default(() => envVars.ROCK_BASE_URL),
  // DEPRECATED: Use extraHeaders instead. Will be removed in the future.
  // To migrate: set extraHeaders = { 'XRL-Authorization': 'Bearer your_token' }
  xrlAuthorization: z.string().optional(),
  extraHeaders: z.record(z.string()).default({}),
});

export type BaseConfig = z.infer<typeof BaseConfigSchema>;

/**
 * Sandbox configuration schema
 */
export const SandboxConfigSchema = BaseConfigSchema.extend({
  image: z.string().default(() => envVars.ROCK_DEFAULT_IMAGE),
  imageOs: z.string().default('linux'),
  autoClearSeconds: z.number().default(() => envVars.ROCK_DEFAULT_AUTO_CLEAR_SECONDS),
  routeKey: z.string().optional(),
  startupTimeout: z.number().default(() => envVars.ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS),
  memory: z.string().default(() => envVars.ROCK_DEFAULT_MEMORY),
  cpus: z.number().default(() => envVars.ROCK_DEFAULT_CPUS),
  limitCpus: z.number().nullable().default(null),
  numGpus: z.number().nullable().default(null),
  acceleratorType: z.string().nullable().default(null),
  userId: z.string().optional(),
  experimentId: z.string().optional(),
  cluster: z.string().default(() => envVars.ROCK_DEFAULT_CLUSTER),
  namespace: z.string().optional(),
  registryUsername: z.string().nullable().default(null),
  registryPassword: z.string().nullable().default(null),
  useKataRuntime: z.boolean().default(false),
  sandboxId: z.string().optional(),
  autoDeleteSeconds: z.number().int().nullable().default(null),
  disk: z.string().nullable().default(null),
});

/**
 * SandboxConfigSchema with autoDeleteSeconds validation applied.
 * Identical to SandboxConfigSchema but with an additional refinement
 * that ensures autoDeleteSeconds >= 0 (matches Python SDK behavior).
 * Use this when you need strict config validation.
 */
export const SandboxConfigSchemaRefined = SandboxConfigSchema.superRefine((data, ctx) => {
  if (data.autoDeleteSeconds !== null && data.autoDeleteSeconds !== undefined && data.autoDeleteSeconds < 0) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: 'autoDeleteSeconds must be >= 0',
      path: ['autoDeleteSeconds'],
    });
  }
});

export type SandboxConfig = z.infer<typeof SandboxConfigSchema>;

/**
 * Sandbox group configuration schema
 */
export const SandboxGroupConfigSchema = SandboxConfigSchema.extend({
  size: z.number().default(() => envVars.ROCK_DEFAULT_GROUP_SIZE),
  startConcurrency: z.number().default(() => envVars.ROCK_DEFAULT_START_CONCURRENCY),
  startRetryTimes: z.number().default(() => envVars.ROCK_DEFAULT_START_RETRY_TIMES),
});

export type SandboxGroupConfig = z.infer<typeof SandboxGroupConfigSchema>;

/**
 * Create sandbox config with defaults
 */
export function createSandboxConfig(
  config?: Partial<SandboxConfig>
): SandboxConfig {
  return SandboxConfigSchemaRefined.parse(config ?? {});
}

/**
 * Create sandbox group config with defaults
 */
export function createSandboxGroupConfig(
  config?: Partial<SandboxGroupConfig>
): SandboxGroupConfig {
  return SandboxGroupConfigSchema.parse(config ?? {});
}
