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

  test('should use exponential backoff by default (backoff=2.0)', async () => {
    // Test that default backoff is 2.0, not 1.0
    // With backoff=2.0 and delaySeconds=0.05:
    //   - After 1st failure: wait 0.05s
    //   - After 2nd failure: wait 0.10s (0.05 * 2)
    //   - Total: ~0.15s (150ms)
    // With backoff=1.0:
    //   - After 1st failure: wait 0.05s
    //   - After 2nd failure: wait 0.05s (0.05 * 1)
    //   - Total: ~0.10s (100ms)
    const fn = jest
      .fn()
      .mockRejectedValueOnce(new Error('fail 1'))
      .mockRejectedValueOnce(new Error('fail 2'))
      .mockRejectedValue(new Error('fail 3'));

    const startTime = Date.now();
    await retryAsync(fn, {
      maxAttempts: 3,
      delaySeconds: 0.05,
      // NOT passing backoff - testing default value
    }).catch(() => {}); // Ignore final error
    const elapsed = Date.now() - startTime;

    // With exponential backoff (2.0), should wait at least 140ms (0.05 + 0.10 = 0.15s)
    // With linear backoff (1.0), would only wait about 100ms (0.05 + 0.05 = 0.10s)
    expect(elapsed).toBeGreaterThanOrEqual(140);
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
