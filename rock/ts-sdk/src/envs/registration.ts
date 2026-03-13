/**
 * Environment factory function
 */

import { RockEnv } from './rock_env.js';

/**
 * Create a Rock environment instance
 *
 * @param envId - Environment ID
 * @param options - Environment options
 * @returns Promise resolving to RockEnv instance
 */
export async function make(
  envId: string,
  options?: Record<string, unknown>
): Promise<RockEnv> {
  return RockEnv.create({ envId, ...options });
}