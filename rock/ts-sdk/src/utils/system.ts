/**
 * System utilities
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
 * Check if running in browser environment
 */
export function isBrowser(): boolean {
  return typeof globalThis !== 'undefined' && 
    'window' in globalThis && 
    typeof (globalThis as Record<string, unknown>).window !== 'undefined';
}

/**
 * Get environment variable
 */
export function getEnv(key: string, defaultValue?: string): string | undefined {
  if (isNode()) {
    return process.env[key] ?? defaultValue;
  }
  return defaultValue;
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
 */
export function isEnvSet(key: string): boolean {
  if (isNode()) {
    return key in process.env;
  }
  return false;
}
