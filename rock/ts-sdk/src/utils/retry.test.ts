/**
 * Tests for Retry utilities
 */

import { retryAsync, sleep, withRetry } from './retry.js';

describe('retryAsync', () => {
  test('should succeed on first attempt', async () => {
    const fn = jest.fn().mockResolvedValue('success');
    const result = await retryAsync(fn);

    expect(result).toBe('success');
    expect(fn).toHaveBeenCalledTimes(1);
  });

  test('should retry on failure', async () => {
    const fn = jest
      .fn()
      .mockRejectedValueOnce(new Error('fail 1'))
      .mockRejectedValueOnce(new Error('fail 2'))
      .mockResolvedValue('success');

    const result = await retryAsync(fn, { maxAttempts: 3, delaySeconds: 0.01 });

    expect(result).toBe('success');
    expect(fn).toHaveBeenCalledTimes(3);
  });

  test('should throw after max attempts', async () => {
    const fn = jest.fn().mockRejectedValue(new Error('always fails'));

    await expect(
      retryAsync(fn, { maxAttempts: 3, delaySeconds: 0.01 })
    ).rejects.toThrow('always fails');

    expect(fn).toHaveBeenCalledTimes(3);
  });

  test('should apply backoff', async () => {
    const fn = jest
      .fn()
      .mockRejectedValueOnce(new Error('fail'))
      .mockResolvedValue('success');

    const startTime = Date.now();
    await retryAsync(fn, {
      maxAttempts: 2,
      delaySeconds: 0.05,
      backoff: 2,
    });
    const elapsed = Date.now() - startTime;

    // Should have waited at least 50ms (0.05s)
    expect(elapsed).toBeGreaterThanOrEqual(40);
  });
});

describe('sleep', () => {
  test('should sleep for specified time', async () => {
    const startTime = Date.now();
    await sleep(50);
    const elapsed = Date.now() - startTime;

    expect(elapsed).toBeGreaterThanOrEqual(40);
  });
});

describe('withRetry', () => {
  test('should wrap function with retry logic', async () => {
    const fn = jest
      .fn()
      .mockRejectedValueOnce(new Error('fail'))
      .mockResolvedValue('success');

    const wrapped = withRetry(fn, { maxAttempts: 2, delaySeconds: 0.01 });
    const result = await wrapped();

    expect(result).toBe('success');
    expect(fn).toHaveBeenCalledTimes(2);
  });
});
