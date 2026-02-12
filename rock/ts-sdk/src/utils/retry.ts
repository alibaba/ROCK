/**
 * Retry utilities
 */

/**
 * Retry options
 */
export interface RetryOptions {
  maxAttempts?: number;
  delaySeconds?: number;
  backoff?: number;
  jitter?: boolean;
}

/**
 * Retry decorator for async functions
 */
export function retryAsync<T>(
  fn: () => Promise<T>,
  options: RetryOptions = {}
): Promise<T> {
  const {
    maxAttempts = 3,
    delaySeconds = 1.0,
    backoff = 1.0,
    jitter = false,
  } = options;

  return retryAsyncImpl(fn, {
    maxAttempts,
    delaySeconds,
    backoff,
    jitter,
  });
}

async function retryAsyncImpl<T>(
  fn: () => Promise<T>,
  options: Required<RetryOptions>
): Promise<T> {
  const { maxAttempts, delaySeconds, backoff, jitter } = options;
  let lastError: Error | null = null;
  let currentDelay = delaySeconds;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (e) {
      lastError = e instanceof Error ? e : new Error(String(e));

      if (attempt === maxAttempts) {
        break;
      }

      let sleepTime = currentDelay;
      if (jitter) {
        sleepTime = Math.random() * currentDelay * 2;
      }

      await sleep(sleepTime * 1000);
      currentDelay *= backoff;
    }
  }

  throw lastError ?? new Error('All retry attempts failed');
}

/**
 * Sleep utility
 */
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Create a retry wrapper for a function
 */
export function withRetry<T extends (...args: unknown[]) => Promise<unknown>>(
  fn: T,
  options: RetryOptions = {}
): T {
  return (async (...args: Parameters<T>) => {
    return retryAsync(() => fn(...args), options);
  }) as T;
}
