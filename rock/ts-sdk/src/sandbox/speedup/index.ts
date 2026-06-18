/**
 * Speedup module for sandbox acceleration
 *
 * Mirrors Python rock/sdk/sandbox/speedup/__init__.py
 */

export { SpeedupType } from './types.js';
export { SpeedupStrategy } from './base.js';
export type { PrecheckResult } from './base.js';
export { SpeedupExecutor } from './executor.js';
export { AptSpeedupStrategy, PipSpeedupStrategy, GithubSpeedupStrategy } from './strategies/index.js';
