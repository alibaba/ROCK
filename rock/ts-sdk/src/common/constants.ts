/**
 * Common constants
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
 * PID prefix and suffix for nohup output parsing
 */
export const PID_PREFIX = '__ROCK_PID_START__';
export const PID_SUFFIX = '__ROCK_PID_END__';
