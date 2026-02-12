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
 * Create RockEnvInfo from plain object
 */
export function createRockEnvInfo(data: Record<string, unknown>): RockEnvInfo {
  return RockEnvInfoSchema.parse({
    envName: data.env_name ?? data.envName,
    image: data.image,
    owner: data.owner ?? '',
    createAt: data.create_at ?? data.createAt ?? '',
    updateAt: data.update_at ?? data.updateAt ?? '',
    description: data.description ?? '',
    tags: data.tags ?? [],
    extraSpec: data.extra_spec ?? data.extraSpec,
  });
}

/**
 * Convert RockEnvInfo to plain object (snake_case for API)
 */
export function rockEnvInfoToDict(env: RockEnvInfo): Record<string, unknown> {
  return {
    env_name: env.envName,
    image: env.image,
    owner: env.owner,
    create_at: env.createAt,
    update_at: env.updateAt,
    description: env.description,
    tags: env.tags,
    extra_spec: env.extraSpec,
  };
}
