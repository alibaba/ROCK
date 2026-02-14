/**
 * Environment factory function
 */

import { RockEnv } from './rock_env.js';

/**
 * Create a Rock environment instance
 *
 * @param envId - Environment ID
 * @param options - Environment options
 * @returns RockEnv instance
 */
export function make(envId: string, options?: Record<string, unknown>): RockEnv {
  return new RockEnv({ envId, ...options });
}
