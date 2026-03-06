/**
 * Sandbox utilities
 */

import { initLogger } from '../logger.js';
import { sleep } from '../utils/retry.js';
import type { Sandbox } from './client.js';
import type { RunModeType } from '../common/constants.js';
import type { Observation } from '../types/responses.js';

const logger = initLogger('rock.sandbox.utils');

/**
 * Get the caller's module name for logger naming
 */
function getCallerLoggerName(): string {
  const stack = new Error().stack;
  if (!stack) return 'unknown';

  const lines = stack.split('\n');
  // Skip first two lines (Error and this function)
  for (let i = 2; i < lines.length; i++) {
    const line = lines[i];
    if (line && !line.includes('utils.ts')) {
      const match = line.match(/at\s+(?:(?:async\s+)?(?:\w+\.)?(\w+)|(\w+))/);
      if (match) {
        return match[1] ?? match[2] ?? 'unknown';
      }
    }
  }
  return 'unknown';
}

/**
 * Decorator to add timing and logging to functions
 */
export function withTimeLogging(operationName: string): MethodDecorator {
  return function (
    target: unknown,
    propertyKey: string | symbol,
    descriptor: PropertyDescriptor
  ): PropertyDescriptor {
    const originalMethod = descriptor.value;

    descriptor.value = async function (...args: unknown[]): Promise<unknown> {
      const startTime = Date.now();
      const name = getCallerLoggerName();
      const log = initLogger(name);

      log.debug(`${operationName} started`);

      try {
        const result = await originalMethod.apply(this, args);
        const elapsed = Date.now() - startTime;
        log.info(`${operationName} completed (elapsed: ${elapsed / 1000}s)`);
        return result;
      } catch (e) {
        const elapsed = Date.now() - startTime;
        log.error(`${operationName} failed: ${e} (elapsed: ${elapsed / 1000}s)`);
        throw e;
      }
    };

    return descriptor;
  };
}

/**
 * Run command with retry
 */
export async function arunWithRetry(
  sandbox: Sandbox,
  cmd: string,
  session: string,
  mode: RunModeType,
  options: {
    waitTimeout?: number;
    waitInterval?: number;
    maxAttempts?: number;
    errorMsg?: string;
  } = {}
): Promise<Observation> {
  const {
    waitTimeout = 300,
    waitInterval = 10,
    maxAttempts = 3,
    errorMsg = 'Command failed',
  } = options;

  let lastError: Error | null = null;
  let currentDelay = 5000;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      const result = await sandbox.arun(cmd, {
        session,
        mode,
        waitTimeout,
        waitInterval,
      });

      if (result.exitCode !== 0) {
        throw new Error(
          `${errorMsg} with exit code: ${result.exitCode}, output: ${result.output}`
        );
      }
      return result;
    } catch (e) {
      lastError = e instanceof Error ? e : new Error(String(e));
      logger.warn(`Attempt ${attempt}/${maxAttempts} failed: ${lastError.message}`);

      if (attempt < maxAttempts) {
        await sleep(currentDelay);
        currentDelay *= 2;
      }
    }
  }

  throw lastError ?? new Error(`${errorMsg}: all attempts failed`);
}

/**
 * Re-export extractNohupPid from utils/http.ts to avoid duplication
 */
export { extractNohupPid } from '../utils/http.js';
