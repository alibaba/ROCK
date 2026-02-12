/**
 * Sandbox types
 */

/**
 * Run mode type
 */
export type RunModeType = 'normal' | 'nohup';

/**
 * Run mode enum
 */
export const RunMode = {
  NORMAL: 'normal' as const,
  NOHUP: 'nohup' as const,
};

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
