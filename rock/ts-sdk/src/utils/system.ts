/**
 * System utilities
 * 
 * Note: This SDK requires Node.js environment (v20.8.0+).
 * It cannot run in browsers due to dependencies on Node.js modules.
 */

/**
 * Check if running in Node.js environment
 */
export function isNode(): boolean {
  return (
    typeof process !== 'undefined' &&
    process.versions != null &&
    process.versions.node != null
  );
}

/**
 * Get environment variable
 * Directly accesses process.env since this SDK requires Node.js
 */
export function getEnv(key: string, defaultValue?: string): string | undefined {
  return process.env[key] ?? defaultValue;
}

/**
 * Get required environment variable
 */
export function getRequiredEnv(key: string): string {
  const value = getEnv(key);
  if (value === undefined) {
    throw new Error(`Required environment variable ${key} is not set`);
  }
  return value;
}

/**
 * Check if environment variable is set
 * Directly checks process.env since this SDK requires Node.js
 */
export function isEnvSet(key: string): boolean {
  return key in process.env;
}
