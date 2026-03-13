/**
 * Integration test for FileSystem operations
 * 
 * Prerequisites:
 * - ROCK_BASE_URL environment variable or default baseUrl
 * - Access to the ROCK sandbox service
 * - Valid Docker image with tar command
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

describe('FileSystem Integration', () => {
  let sandbox: Sandbox;
  let tempDir: string;

  beforeEach(async () => {
    sandbox = new Sandbox(TEST_CONFIG);
    await sandbox.start();
    
    // Create default session for NORMAL mode commands (new behavior: arun() no longer auto-creates sessions)
    await sandbox.createSession({ session: 'default', startupSource: [], envEnable: false });
    
    // Create a temporary directory for test files
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rock-test-'));
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

  describe('uploadDir', () => {
    test('should upload a directory to sandbox', async () => {
      // Arrange: Create a local directory with files
      const sourceDir = path.join(tempDir, 'source');
      fs.mkdirSync(sourceDir, { recursive: true });
      fs.writeFileSync(path.join(sourceDir, 'file1.txt'), 'Hello World');
      fs.writeFileSync(path.join(sourceDir, 'file2.txt'), 'Test Content');
      fs.mkdirSync(path.join(sourceDir, 'subdir'), { recursive: true });
      fs.writeFileSync(path.join(sourceDir, 'subdir', 'file3.txt'), 'Nested File');

      const targetDir = '/tmp/uploaded_test_dir';

      // Act: Upload the directory
      const result = await sandbox.getFs().uploadDir(sourceDir, targetDir);

      // Assert: Upload should succeed
      expect(result.exitCode).toBe(0);
      expect(result.failureReason).toBe('');

      // Assert: Files should exist in sandbox
      const listResult = await sandbox.arun(`ls -la ${targetDir}`, { mode: RunMode.NORMAL });
      expect(listResult.exitCode).toBe(0);
      expect(listResult.output).toContain('file1.txt');
      expect(listResult.output).toContain('file2.txt');
      expect(listResult.output).toContain('subdir');

      // Assert: File contents should match
      const contentResult = await sandbox.arun(`cat ${targetDir}/file1.txt`, { mode: RunMode.NORMAL });
      expect(contentResult.output.trim()).toBe('Hello World');

      // Assert: Nested files should exist
      const nestedResult = await sandbox.arun(`cat ${targetDir}/subdir/file3.txt`, { mode: RunMode.NORMAL });
      expect(nestedResult.output.trim()).toBe('Nested File');
    }, 180000);

    test('should return error when source directory does not exist', async () => {
      const nonExistentDir = path.join(tempDir, 'nonexistent');
      const targetDir = '/tmp/should_not_exist';

      const result = await sandbox.getFs().uploadDir(nonExistentDir, targetDir);

      expect(result.exitCode).toBe(1);
      expect(result.failureReason).toContain('source_dir not found');
    });

    test('should return error when source is not a directory', async () => {
      const filePath = path.join(tempDir, 'notadir.txt');
      fs.writeFileSync(filePath, 'I am a file, not a directory');
      const targetDir = '/tmp/should_not_exist';

      const result = await sandbox.getFs().uploadDir(filePath, targetDir);

      expect(result.exitCode).toBe(1);
      expect(result.failureReason).toContain('source_dir must be a directory');
    });

    test('should return error when target path is not absolute', async () => {
      const sourceDir = path.join(tempDir, 'source');
      fs.mkdirSync(sourceDir, { recursive: true });
      fs.writeFileSync(path.join(sourceDir, 'file.txt'), 'content');

      const relativeTarget = 'relative/path';

      const result = await sandbox.getFs().uploadDir(sourceDir, relativeTarget);

      expect(result.exitCode).toBe(1);
      expect(result.failureReason).toMatch(/must be absolute|Path must be absolute/i);
    });

    test('should overwrite existing target directory', async () => {
      // Arrange: Create source directory
      const sourceDir = path.join(tempDir, 'source');
      fs.mkdirSync(sourceDir, { recursive: true });
      fs.writeFileSync(path.join(sourceDir, 'newfile.txt'), 'New Content');

      const targetDir = '/tmp/overwrite_test_dir';

      // Create existing directory in sandbox with different content
      await sandbox.arun(`mkdir -p ${targetDir}`, { mode: RunMode.NORMAL });
      await sandbox.arun(`echo "old content" > ${targetDir}/oldfile.txt`, { mode: RunMode.NORMAL });

      // Act: Upload should succeed and overwrite
      const result = await sandbox.getFs().uploadDir(sourceDir, targetDir);

      // Assert: Upload should succeed
      expect(result.exitCode).toBe(0);

      // Assert: New file should exist
      const newFileResult = await sandbox.arun(`cat ${targetDir}/newfile.txt`, { mode: RunMode.NORMAL });
      expect(newFileResult.output.trim()).toBe('New Content');

      // Assert: Old file should no longer exist (directory was replaced)
      // Use test command instead of ls to avoid throwing on non-existent file
      const oldFileCheck = await sandbox.arun(`test -f ${targetDir}/oldfile.txt && echo "exists" || echo "not exists"`, { mode: RunMode.NORMAL });
      expect(oldFileCheck.output.trim()).toBe('not exists');
    }, 180000);
  });
});
