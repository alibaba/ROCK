/**
 * Trial sub-module: AbstractTrial, registry, and concrete trial implementations.
 */

export { AbstractTrial } from './abstract.js';
export type { ISandbox } from './abstract.js';
export { registerTrial, createTrial, _assignRegistryKey } from './registry.js';
export { BashTrial, BASH_JOB_CONFIG_KEY } from './bash.js';
export { HarborTrial, HARBOR_JOB_CONFIG_KEY } from './harbor.js';
export { ComposeTrial, COMPOSE_JOB_CONFIG_KEY } from './compose.js';
