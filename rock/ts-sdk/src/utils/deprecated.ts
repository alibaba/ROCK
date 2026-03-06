/**
 * Deprecated decorator utilities
 */

import { initLogger } from '../logger.js';
import type { Logger } from '../logger.js';

/**
 * Set to track which deprecation warnings have already been shown
 */
const warnedKeys = new Set<string>();

/**
 * Get or create the deprecation logger (lazy initialization)
 */
function getLogger(): Logger {
  return initLogger('rock.deprecated');
}

/**
 * Clear all deprecation warning states (useful for testing)
 */
export function clearDeprecatedWarnings(): void {
  warnedKeys.clear();
}

/**
 * Issue a deprecation warning only once per key
 */
function warnOnce(key: string, message: string): void {
  if (warnedKeys.has(key)) {
    return;
  }
  getLogger().warn(message);
  warnedKeys.add(key);
}

/**
 * Mark a function as deprecated
 */
export function deprecated(reason: string = ''): MethodDecorator {
  return function (
    target: unknown,
    propertyKey: string | symbol,
    descriptor: PropertyDescriptor
  ): PropertyDescriptor {
    const originalMethod = descriptor.value;
    const key = String(propertyKey);

    descriptor.value = function (...args: unknown[]): unknown {
      warnOnce(key, `${key} is deprecated. ${reason}`);
      return originalMethod.apply(this, args);
    };

    return descriptor;
  };
}

/**
 * Mark a class as deprecated
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function deprecatedClass(reason: string = ''): <T extends new (...args: any[]) => any>(constructor: T) => T {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return function <T extends new (...args: any[]) => any>(
    constructor: T
  ): T {
    const key = constructor.name;
    return class extends constructor {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      constructor(...args: any[]) {
        warnOnce(key, `${key} is deprecated. ${reason}`);
        super(...args);
      }
    };
  };
}
