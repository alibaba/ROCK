/**
 * Sandbox types
 */

// Re-export RunModeType and RunMode from constants to avoid duplication
export { RunModeType, RunMode } from '../common/constants.js';

/**
 * Speedup type enum
 */
export enum SpeedupType {
  APT = 'apt',
  PIP = 'pip',
  GITHUB = 'github',
}

/**
 * Runtime environment ID type
 */
export type RuntimeEnvId = string;

/**
 * Agent type
 */
export type AgentType = 'default' | 'iflow-cli' | 'openhands' | 'swe-agent';
