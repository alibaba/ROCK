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
  xrlAuthorization: z.string().optional(),
  extraHeaders: z.record(z.string()).default({}),
});

export type BaseConfig = z.infer<typeof BaseConfigSchema>;

/**
 * Sandbox configuration schema
 */
export const SandboxConfigSchema = BaseConfigSchema.extend({
  image: z.string().default(() => envVars.ROCK_DEFAULT_IMAGE),
  autoClearSeconds: z.number().default(() => envVars.ROCK_DEFAULT_AUTO_CLEAR_SECONDS),
  routeKey: z.string().optional(),
  startupTimeout: z.number().default(() => envVars.ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS),
  memory: z.string().default(() => envVars.ROCK_DEFAULT_MEMORY),
  cpus: z.number().default(() => envVars.ROCK_DEFAULT_CPUS),
  userId: z.string().optional(),
  experimentId: z.string().optional(),
  cluster: z.string().default(() => envVars.ROCK_DEFAULT_CLUSTER),
  namespace: z.string().optional(),
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
  return SandboxConfigSchema.parse(config ?? {});
}

/**
 * Create sandbox group config with defaults
 */
export function createSandboxGroupConfig(
  config?: Partial<SandboxGroupConfig>
): SandboxGroupConfig {
  return SandboxGroupConfigSchema.parse(config ?? {});
}
