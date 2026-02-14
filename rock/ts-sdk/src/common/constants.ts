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
 * Constants class (deprecated, use envs module instead)
 * @deprecated Use envs module instead
 */
export class Constants {
  static readonly BASE_URL_PRODUCT = '';
  static readonly BASE_URL_ALIYUN = '';
  static readonly BASE_URL_INNER = '';
  static readonly BASE_URL_PRE = '';
  static readonly BASE_URL_LOCAL = '';
  static readonly REQUEST_TIMEOUT_SECONDS = 180;
}

/**
 * PID prefix and suffix for nohup output parsing
 */
export const PID_PREFIX = '__ROCK_PID_START__';
export const PID_SUFFIX = '__ROCK_PID_END__';
