/**
 * Tests for FileSystem - Injection Protection
 */

import { LinuxFileSystem } from './file_system.js';
import type { AbstractSandbox } from './client.js';
import type { Observation, CommandResponse } from '../types/responses.js';
import { existsSync, statSync } from 'fs';

// Mock fs module
jest.mock('fs', () => ({
  existsSync: jest.fn(),
  statSync: jest.fn(),
  mkdtempSync: jest.fn().mockReturnValue('/tmp/rock-upload-test'),
  rmSync: jest.fn(),
}));

// Mock child_process
jest.mock('child_process', () => ({
  spawn: jest.fn().mockImplementation(() => ({
    stderr: { on: jest.fn() },
    on: jest.fn((event, callback) => {
      if (event === 'close') callback(0);
    }),
  })),
}));

// Mock sandbox factory
function createMockSandbox(): jest.Mocked<AbstractSandbox> {
  return {
    getSandboxId: jest.fn().mockReturnValue('test-sandbox'),
    execute: jest.fn().mockResolvedValue({
      stdout: '',
      stderr: '',
      exitCode: 0,
    } as CommandResponse),
    arun: jest.fn().mockResolvedValue({
      output: '',
      exitCode: 0,
      failureReason: '',
      expectString: '',
    } as Observation),
    createSession: jest.fn().mockResolvedValue(undefined),
    upload: jest.fn().mockResolvedValue({ success: true, message: '' }),
  } as unknown as jest.Mocked<AbstractSandbox>;
}

describe('FileSystem', () => {
  describe('uploadDir injection protection', () => {
    let fs: LinuxFileSystem;
    let mockSandbox: jest.Mocked<AbstractSandbox>;

    beforeEach(() => {
      mockSandbox = createMockSandbox();
      fs = new LinuxFileSystem(mockSandbox);
      // Mock source directory exists
      (existsSync as jest.Mock).mockReturnValue(true);
      (statSync as jest.Mock).mockReturnValue({ isDirectory: () => true });
    });

    test('should reject path traversal attempts in targetDir', async () => {
      const result = await fs.uploadDir('/tmp/source', '/tmp/../../../etc/passwd');
      // Should either reject or sanitize the path
      expect(result.exitCode).toBe(1);
      expect(result.failureReason).toMatch(/invalid|traversal|not allowed|\.\./i);
    });

    test('should reject paths containing shell metacharacters', async () => {
      const result = await fs.uploadDir('/tmp/source', "/tmp/dir$(rm -rf /)");
      expect(result.exitCode).toBe(1);
      expect(result.failureReason).toMatch(/invalid|character|not allowed|\$|forbidden/i);
    });

    test('should reject paths containing backticks', async () => {
      const result = await fs.uploadDir('/tmp/source', '/tmp/`whoami`');
      expect(result.exitCode).toBe(1);
      expect(result.failureReason).toMatch(/invalid|character|not allowed|`|forbidden/i);
    });

    test('should reject paths containing semicolons', async () => {
      const result = await fs.uploadDir('/tmp/source', '/tmp/dir; rm -rf /');
      expect(result.exitCode).toBe(1);
      expect(result.failureReason).toMatch(/invalid|character|not allowed|forbidden/i);
    });

    test('should reject paths containing pipe characters', async () => {
      const result = await fs.uploadDir('/tmp/source', '/tmp/dir | cat /etc/passwd');
      expect(result.exitCode).toBe(1);
      expect(result.failureReason).toMatch(/invalid|character|not allowed|forbidden/i);
    });

    test('should reject paths containing && or ||', async () => {
      const result = await fs.uploadDir('/tmp/source', '/tmp/dir && rm -rf /');
      expect(result.exitCode).toBe(1);
      expect(result.failureReason).toMatch(/invalid|character|not allowed|forbidden/i);
    });

    test('should accept valid absolute paths', async () => {
      const result = await fs.uploadDir('/tmp/source', '/tmp/valid/path');
      // Should succeed (mocked source exists)
      expect(result.exitCode).toBe(0);
    });
  });

  describe('chown injection protection', () => {
    let fs: LinuxFileSystem;
    let mockSandbox: jest.Mocked<AbstractSandbox>;

    beforeEach(() => {
      mockSandbox = createMockSandbox();
      fs = new LinuxFileSystem(mockSandbox);
    });

    test('should reject remoteUser with shell metacharacters', async () => {
      await expect(fs.chown({
        paths: ['/tmp/test'],
        recursive: false,
        remoteUser: 'root; rm -rf /',
      })).rejects.toThrow(/invalid|character|not allowed/i);
    });

    test('should reject remoteUser starting with dash', async () => {
      await expect(fs.chown({
        paths: ['/tmp/test'],
        recursive: false,
        remoteUser: '-rf',
      })).rejects.toThrow(/invalid|character|not allowed/i);
    });

    test('should reject paths containing shell metacharacters', async () => {
      await expect(fs.chown({
        paths: ['/tmp/test$(whoami)'],
        recursive: false,
        remoteUser: 'root',
      })).rejects.toThrow(/invalid|character|not allowed/i);
    });

    test('should accept valid usernames', async () => {
      await fs.chown({
        paths: ['/tmp/test'],
        recursive: false,
        remoteUser: 'validuser',
      });
      expect(mockSandbox.execute).toHaveBeenCalled();
    });

    test('should accept valid usernames with underscore', async () => {
      await fs.chown({
        paths: ['/tmp/test'],
        recursive: false,
        remoteUser: 'valid_user',
      });
      expect(mockSandbox.execute).toHaveBeenCalled();
    });
  });

  describe('chmod injection protection', () => {
    let fs: LinuxFileSystem;
    let mockSandbox: jest.Mocked<AbstractSandbox>;

    beforeEach(() => {
      mockSandbox = createMockSandbox();
      fs = new LinuxFileSystem(mockSandbox);
    });

    test('should reject invalid mode format', async () => {
      await expect(fs.chmod({
        paths: ['/tmp/test'],
        recursive: false,
        mode: '755; rm -rf /',
      })).rejects.toThrow(/invalid|mode/i);
    });

    test('should reject mode with non-octal characters', async () => {
      await expect(fs.chmod({
        paths: ['/tmp/test'],
        recursive: false,
        mode: 'abc',
      })).rejects.toThrow(/invalid|mode/i);
    });

    test('should reject paths containing shell metacharacters', async () => {
      await expect(fs.chmod({
        paths: ['/tmp/test$(id)'],
        recursive: false,
        mode: '755',
      })).rejects.toThrow(/invalid|character|not allowed/i);
    });

    test('should accept valid octal mode', async () => {
      await fs.chmod({
        paths: ['/tmp/test'],
        recursive: false,
        mode: '755',
      });
      expect(mockSandbox.execute).toHaveBeenCalled();
    });

    test('should accept valid symbolic mode', async () => {
      await fs.chmod({
        paths: ['/tmp/test'],
        recursive: false,
        mode: 'u+x',
      });
      expect(mockSandbox.execute).toHaveBeenCalled();
    });
  });
});
