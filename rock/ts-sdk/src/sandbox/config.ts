/**
 * Sandbox configuration
 */

import { z } from 'zod';
import { envVars } from '../env_vars.js';

/**
 * Base configuration schema
 */
export const BaseConfigSchema = z.object({
  baseUrl: z.string().default(envVars.ROCK_BASE_URL),
  xrlAuthorization: z.string().optional(),
  extraHeaders: z.record(z.string()).default({}),
});

export type BaseConfig = z.infer<typeof BaseConfigSchema>;

/**
 * Sandbox configuration schema
 */
export const SandboxConfigSchema = BaseConfigSchema.extend({
  image: z.string().default('python:3.11'),
  autoClearSeconds: z.number().default(300),
  routeKey: z.string().optional(),
  startupTimeout: z.number().default(envVars.ROCK_SANDBOX_STARTUP_TIMEOUT_SECONDS),
  memory: z.string().default('8g'),
  cpus: z.number().default(2),
  userId: z.string().optional(),
  experimentId: z.string().optional(),
  cluster: z.string().default('zb'),
  namespace: z.string().optional(),
});

export type SandboxConfig = z.infer<typeof SandboxConfigSchema>;

/**
 * Sandbox group configuration schema
 */
export const SandboxGroupConfigSchema = SandboxConfigSchema.extend({
  size: z.number().default(2),
  startConcurrency: z.number().default(2),
  startRetryTimes: z.number().default(3),
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
