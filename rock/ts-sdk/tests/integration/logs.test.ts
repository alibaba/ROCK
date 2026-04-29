/**
 * Integration test for Sandbox Logs operations
 * 
 * Prerequisites:
 * - ROCK_BASE_URL environment variable or default baseUrl
 * - Access to the ROCK sandbox service
 * - ROCK_OSS_ENABLE=true for downloadLog tests
 * - Valid OSS configuration (ROCK_OSS_BUCKET_NAME, ROCK_OSS_BUCKET_REGION, etc.)
 */

import { Sandbox, RunMode } from '../../src';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

const TEST_CONFIG = {
  baseUrl: process.env.ROCK_BASE_URL || 'http://11.166.8.116:8080',
  image: 'reg.docker.alibaba-inc.com/yanan/python:3.11',
  cluster: 'zb',
  startupTimeout: 120,
};

describe('Sandbox Logs Integration', () => {
  let sandbox: Sandbox;
  let tempDir: string;

  beforeEach(async () => {
    sandbox = new Sandbox(TEST_CONFIG);
    await sandbox.start();
    
    // Create default session
    await sandbox.createSession({ session: 'default', startupSource: [], envEnable: false });
    
    // Create test log files in sandbox
    await sandbox.arun('mkdir -p /data/logs', { mode: RunMode.NORMAL });
    await sandbox.arun('echo "test log content" > /data/logs/test.log', { mode: RunMode.NORMAL });
    await sandbox.arun('echo "error log" > /data/logs/error.log', { mode: RunMode.NORMAL });
    await sandbox.arun('mkdir -p /data/logs/subdir', { mode: RunMode.NORMAL });
    await sandbox.arun('echo "nested log" > /data/logs/subdir/nested.log', { mode: RunMode.NORMAL });
    
    // Create a temporary directory for downloaded files
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rock-logs-test-'));
  }, 180000); // 3 minutes timeout for sandbox startup

  afterEach(async () => {
    // Cleanup: ensure sandbox is stopped even if test fails
    if (sandbox) {
      try {
        await sandbox.close();
      } catch (e) {
        // Ignore cleanup errors
      }
    }
    
    // Cleanup local temp directory
    if (tempDir && fs.existsSync(tempDir)) {
      try {
        fs.rmSync(tempDir, { recursive: true, force: true });
      } catch (e) {
        // Ignore cleanup errors
      }
    }
  });

  describe('listLogs - Alive Sandbox', () => {
    test('should list all log files recursively', async () => {
      // Act: List all logs
      const files = await sandbox.listLogs();

      // Assert: Should find all log files
      expect(files.length).toBeGreaterThanOrEqual(3);
      const fileNames = files.map(f => f.name);
      expect(fileNames).toContain('test.log');
      expect(fileNames).toContain('error.log');
      expect(fileNames).toContain('nested.log');
    }, 60000);

    test('should list files with pattern filter', async () => {
      // Act: List only *.log files (should include all since they're all .log)
      const files = await sandbox.listLogs({ pattern: '*.log' });

      // Assert: Should find log files matching pattern
      expect(files.length).toBeGreaterThanOrEqual(3);
      const fileNames = files.map(f => f.name);
      expect(fileNames).toContain('test.log');
      expect(fileNames).toContain('error.log');
    }, 60000);

    test('should list files non-recursively', async () => {
      // Act: List only top-level files (no recursion)
      const files = await sandbox.listLogs({ recursive: false });

      // Assert: Should find only top-level files (test.log, error.log)
      const fileNames = files.map(f => f.name);
      expect(fileNames).toContain('test.log');
      expect(fileNames).toContain('error.log');
      // Should NOT include nested.log
      expect(fileNames).not.toContain('nested.log');
    }, 60000);

    test('should return file metadata', async () => {
      // Act
      const files = await sandbox.listLogs({ pattern: 'test.log' });

      // Assert: File metadata should be present
      expect(files.length).toBeGreaterThanOrEqual(1);
      const testLog = files.find(f => f.name === 'test.log');
      expect(testLog).toBeDefined();
      expect(testLog!.size).toBeGreaterThan(0);
      expect(testLog!.modifiedTime).toBeTruthy();
      expect(testLog!.path).toBe('test.log');
      expect(testLog!.isDirectory).toBe(false);
    }, 60000);

    test('should return empty array for empty directory', async () => {
      // Arrange: Create empty log directory
      await sandbox.arun('rm -rf /data/logs/*', { mode: RunMode.NORMAL });

      // Act
      const files = await sandbox.listLogs();

      // Assert
      expect(files).toEqual([]);
    }, 60000);
  });

  describe('downloadLog - Alive Sandbox', () => {
    // Skip these tests if OSS is not enabled
    const ossEnabled = process.env.ROCK_OSS_ENABLE === 'true';
    const conditionalTest = ossEnabled ? test : test.skip;

    conditionalTest('should download a log file to local', async () => {
      // Arrange
      const localPath = path.join(tempDir, 'downloaded.log');

      // Act
      const result = await sandbox.downloadLog('test.log', localPath);

      // Assert
      expect(result.success).toBe(true);
      expect(fs.existsSync(localPath)).toBe(true);
      const content = fs.readFileSync(localPath, 'utf-8');
      expect(content).toContain('test log content');
    }, 120000);

    conditionalTest('should download a nested log file', async () => {
      // Arrange
      const localPath = path.join(tempDir, 'nested.log');

      // Act
      const result = await sandbox.downloadLog('subdir/nested.log', localPath);

      // Assert
      expect(result.success).toBe(true);
      expect(fs.existsSync(localPath)).toBe(true);
      const content = fs.readFileSync(localPath, 'utf-8');
      expect(content).toContain('nested log');
    }, 120000);

    test('should reject absolute path', async () => {
      // Arrange
      const localPath = path.join(tempDir, 'test.log');

      // Act & Assert
      await expect(
        sandbox.downloadLog('/data/logs/test.log', localPath)
      ).rejects.toThrow('logPath must be relative path');
    }, 60000);

    test('should reject path with directory traversal', async () => {
      // Arrange
      const localPath = path.join(tempDir, 'test.log');

      // Act & Assert
      await expect(
        sandbox.downloadLog('../etc/passwd', localPath)
      ).rejects.toThrow("logPath cannot contain '..'");
    }, 60000);
  });
});

/**
 * Integration tests for destroyed sandbox logs
 * 
 * NOTE: These tests require Kmon API access which may not be available in CI.
 * They are intended to be run manually with proper Kmon configuration:
 * - ROCK_KMON_TOKEN environment variable
 * - Network access to kmon-metric.alibaba-inc.com
 */
describe.skip('Sandbox Logs Integration - Destroyed Sandbox', () => {
  // These tests require a previously destroyed sandbox and Kmon access
  // They are skipped by default as they need manual setup

  test('should list logs from destroyed sandbox via Kmon', async () => {
    // Setup: Configure Kmon resolver
    await Sandbox.useKmonHostIpResolver();

    // Create a sandbox instance without starting
    // (in real scenario, you would have the sandboxId of a destroyed sandbox)
    const sandbox = new Sandbox({
      ...TEST_CONFIG,
      // sandboxId would be set from a destroyed sandbox
    });

    // This test requires a real destroyed sandbox to work
    // The resolver would query Kmon to get the hostIp
  });

  test('should download log from destroyed sandbox via Kmon', async () => {
    // Similar to above, requires real destroyed sandbox
  });
});
