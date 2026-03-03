/**
 * Tests for ModelClient timeout and cancellation support
 *
 * Following TDD: These tests are written BEFORE the implementation.
 * They should FAIL initially, then we implement the feature to make them pass.
 */

import { existsSync, mkdirSync, rmSync, writeFileSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import { ModelClient, PollOptions } from './client.js';

// Test markers (must match client.ts)
const REQUEST_START_MARKER = '__REQUEST_START__';
const REQUEST_END_MARKER = '__REQUEST_END__';
const SESSION_END_MARKER = '__SESSION_END__';

describe('ModelClient timeout and cancellation', () => {
  let testDir: string;
  let logFile: string;
  let client: ModelClient;

  beforeEach(() => {
    testDir = join(tmpdir(), `model-client-test-${Date.now()}`);
    mkdirSync(testDir, { recursive: true });
    logFile = join(testDir, 'model.log');
    client = new ModelClient({ logFileName: logFile });
  });

  afterEach(() => {
    rmSync(testDir, { recursive: true, force: true });
  });

  describe('popRequest timeout', () => {
    it('should throw TimeoutError when timeout expires', async () => {
      // Create log file with a request that has different index
      const content = `${REQUEST_START_MARKER}test-request${REQUEST_END_MARKER}{"index":0}\n`;
      writeFileSync(logFile, content);

      // Request index 1, but only index 0 exists - should timeout
      await expect(client.popRequest(1, { timeout: 0.1 })).rejects.toThrow('popRequest timed out');
    });

    it('should return request when found before timeout', async () => {
      // Create log file with the request we want
      const content = `${REQUEST_START_MARKER}test-request${REQUEST_END_MARKER}{"index":1}\n`;
      writeFileSync(logFile, content);

      const result = await client.popRequest(1, { timeout: 5 });
      expect(result).toBe('test-request');
    });

    it('should return SESSION_END_MARKER when session ends', async () => {
      const content = `${SESSION_END_MARKER}\n`;
      writeFileSync(logFile, content);

      const result = await client.popRequest(1, { timeout: 1 });
      expect(result).toBe(SESSION_END_MARKER);
    });
  });

  describe('waitForFirstRequest timeout', () => {
    it('should throw TimeoutError when timeout expires and no request arrives', async () => {
      // Don't create log file - it doesn't exist
      await expect(client.waitForFirstRequest({ timeout: 0.1 })).rejects.toThrow('waitForFirstRequest timed out');
    });

    it('should return when log file has content before timeout', async () => {
      // Create log file with content
      writeFileSync(logFile, 'some content\n');

      await expect(client.waitForFirstRequest({ timeout: 5 })).resolves.toBeUndefined();
    });
  });

  describe('popRequest cancellation via AbortSignal', () => {
    it('should throw AbortError when signal is aborted', async () => {
      // Create log file with a request that has different index
      const content = `${REQUEST_START_MARKER}test-request${REQUEST_END_MARKER}{"index":0}\n`;
      writeFileSync(logFile, content);

      const controller = new AbortController();

      // Abort after a short delay
      setTimeout(() => controller.abort(), 100);

      await expect(client.popRequest(1, { timeout: 10, signal: controller.signal })).rejects.toThrow();
    });

    it('should return request when found before abort', async () => {
      // Create log file with the request we want
      const content = `${REQUEST_START_MARKER}test-request${REQUEST_END_MARKER}{"index":1}\n`;
      writeFileSync(logFile, content);

      const controller = new AbortController();

      const result = await client.popRequest(1, { timeout: 5, signal: controller.signal });
      expect(result).toBe('test-request');
    });
  });

  describe('waitForFirstRequest cancellation via AbortSignal', () => {
    it('should throw AbortError when signal is aborted', async () => {
      const controller = new AbortController();

      // Abort after a short delay
      setTimeout(() => controller.abort(), 100);

      await expect(client.waitForFirstRequest({ timeout: 10, signal: controller.signal })).rejects.toThrow();
    });

    it('should return when log file has content before abort', async () => {
      // Create log file with content
      writeFileSync(logFile, 'some content\n');

      const controller = new AbortController();

      await expect(client.waitForFirstRequest({ timeout: 5, signal: controller.signal })).resolves.toBeUndefined();
    });
  });

  describe('backward compatibility', () => {
    it('should use default timeout when not specified', async () => {
      // This test verifies backward compatibility
      // Default timeout should be applied (not infinite wait)
      // We'll test that the method signature accepts no options
      const content = `${REQUEST_START_MARKER}test-request${REQUEST_END_MARKER}{"index":1}\n`;
      writeFileSync(logFile, content);

      // Should work without timeout option
      const result = await client.popRequest(1);
      expect(result).toBe('test-request');
    });

    it('waitForFirstRequest should work without options', async () => {
      writeFileSync(logFile, 'some content\n');

      await expect(client.waitForFirstRequest()).resolves.toBeUndefined();
    });
  });
});
