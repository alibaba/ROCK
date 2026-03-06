/**
 * EnvHub data model definitions
 */

import { z } from 'zod';
import { envVars } from '../env_vars.js';

/**
 * EnvHub client configuration
 */
export const EnvHubClientConfigSchema = z.object({
  baseUrl: z.string().default(envVars.ROCK_ENVHUB_BASE_URL),
});

export type EnvHubClientConfig = z.infer<typeof EnvHubClientConfigSchema>;

/**
 * Rock environment info
 */
export const RockEnvInfoSchema = z.object({
  envName: z.string(),
  image: z.string(),
  owner: z.string().default(''),
  createAt: z.string().default(''),
  updateAt: z.string().default(''),
  description: z.string().default(''),
  tags: z.array(z.string()).default([]),
  extraSpec: z.record(z.unknown()).optional(),
});

export type RockEnvInfo = z.infer<typeof RockEnvInfoSchema>;

/**
 * Create RockEnvInfo from API response (already camelCase after HTTP layer conversion)
 */
export function createRockEnvInfo(data: Record<string, unknown>): RockEnvInfo {
  return RockEnvInfoSchema.parse(data);
}