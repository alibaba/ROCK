/**
 * Tests for Sandbox Client - Exception handling
 */

import axios from 'axios';
import { Sandbox } from './client.js';
import { RunMode } from '../common/constants.js';
import {
  BadRequestRockError,
  InternalServerRockError,
  CommandRockError,
  RockException,
} from '../common/exceptions.js';
import { Codes } from '../types/codes.js';

// Mock axios
jest.mock('axios');
const mockedAxios = axios as jest.Mocked<typeof axios>;

// Helper to create mock axios response
function createMockPost(data: unknown, headers: Record<string, string> = {}) {
  return jest.fn().mockResolvedValue({
    data,
    headers,
  });
}

// Helper to create mock axios get
function createMockGet(data: unknown, headers: Record<string, string> = {}) {
  return jest.fn().mockResolvedValue({
    data,
    headers,
  });
}

describe('Sandbox Exception Handling', () => {
  let sandbox: Sandbox;
  let mockPost: jest.Mock;
  let mockGet: jest.Mock;

  beforeEach(() => {
    jest.clearAllMocks();
    mockPost = jest.fn();
    mockGet = jest.fn();
    mockedAxios.create = jest.fn().mockReturnValue({
      post: mockPost,
      get: mockGet,
    });

    sandbox = new Sandbox({
      image: 'test:latest',
      startupTimeout: 2, // Short timeout for tests
    });
  });

  describe('start() - error code handling', () => {
    test('should throw BadRequestRockError when API returns 4xxx code', async () => {
      // Mock the start_async API to return an error response with code
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Failed',
          result: {
            sandbox_id: 'test-id',
            code: Codes.BAD_REQUEST,
          },
        },
        headers: {},
      });

      await expect(sandbox.start()).rejects.toThrow(BadRequestRockError);
    });

    test('should throw InternalServerRockError when API returns 5xxx code', async () => {
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Failed',
          result: {
            sandbox_id: 'test-id',
            code: Codes.INTERNAL_SERVER_ERROR,
          },
        },
        headers: {},
      });

      await expect(sandbox.start()).rejects.toThrow(InternalServerRockError);
    });

    test('should throw CommandRockError when API returns 6xxx code', async () => {
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Failed',
          result: {
            sandbox_id: 'test-id',
            code: Codes.COMMAND_ERROR,
          },
        },
        headers: {},
      });

      await expect(sandbox.start()).rejects.toThrow(CommandRockError);
    });

    test('should throw RockException for unknown error codes', async () => {
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Failed',
          result: {
            sandbox_id: 'test-id',
            code: 7000,
          },
        },
        headers: {},
      });

      await expect(sandbox.start()).rejects.toThrow(RockException);
    });

    test('should throw InternalServerRockError on startup timeout', async () => {
      // Mock successful start_async but sandbox never becomes alive
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            host_name: 'test-host',
            host_ip: '127.0.0.1',
          },
        },
        headers: {},
      });

      // Mock getStatus to return not alive
      mockGet.mockResolvedValue({
        data: {
          status: 'Success',
          result: {
            is_alive: false,
          },
        },
        headers: {},
      });

      await expect(sandbox.start()).rejects.toThrow(InternalServerRockError);
    }, 10000); // 10s timeout for this test
  });

  describe('execute() - error code handling', () => {
    test('should throw BadRequestRockError when API returns 4xxx code', async () => {
      // First start the sandbox
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            host_name: 'test-host',
            host_ip: '127.0.0.1',
          },
        },
        headers: {},
      });
      mockGet.mockResolvedValue({
        data: {
          status: 'Success',
          result: { is_alive: true },
        },
        headers: {},
      });
      await sandbox.start();

      // Mock execute to return error
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Failed',
          result: {
            code: Codes.BAD_REQUEST,
          },
        },
        headers: {},
      });

      await expect(sandbox.execute({ command: 'test', timeout: 60 })).rejects.toThrow(BadRequestRockError);
    });
  });

  describe('createSession() - error code handling', () => {
    test('should throw BadRequestRockError when API returns 4xxx code', async () => {
      // First start the sandbox
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            host_name: 'test-host',
            host_ip: '127.0.0.1',
          },
        },
        headers: {},
      });
      mockGet.mockResolvedValue({
        data: {
          status: 'Success',
          result: { is_alive: true },
        },
        headers: {},
      });
      await sandbox.start();

      // Mock createSession to return error
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Failed',
          result: {
            code: Codes.BAD_REQUEST,
          },
        },
        headers: {},
      });

      await expect(sandbox.createSession({ 
        session: 'test', 
        startupSource: [], 
        envEnable: false
      })).rejects.toThrow(BadRequestRockError);
    });
  });
});

/**
 * Zod Validation Tests
 * 
 * These tests verify that API responses are validated against Zod schemas.
 * Similar to Python SDK's Pydantic validation: CommandResponse(**result)
 */
import { ZodError } from 'zod';

describe('Zod Validation', () => {
  let sandbox: Sandbox;
  let mockPost: jest.Mock;
  let mockGet: jest.Mock;

  beforeEach(() => {
    jest.clearAllMocks();
    mockPost = jest.fn();
    mockGet = jest.fn();
    mockedAxios.create = jest.fn().mockReturnValue({
      post: mockPost,
      get: mockGet,
    });

    sandbox = new Sandbox({
      image: 'test:latest',
      startupTimeout: 2,
    });
  });

  describe('execute() - Zod validation', () => {
    beforeEach(async () => {
      // Start the sandbox first
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            host_name: 'test-host',
            host_ip: '127.0.0.1',
          },
        },
        headers: {},
      });
      mockGet.mockResolvedValue({
        data: {
          status: 'Success',
          result: { is_alive: true },
        },
        headers: {},
      });
      await sandbox.start();
    });

    test('should throw ZodError when stdout is not a string', async () => {
      // Return invalid data: stdout as number instead of string
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            stdout: 12345, // Invalid: should be string
            stderr: '',
            exit_code: 0,
          },
        },
        headers: {},
      });

      await expect(sandbox.execute({ command: 'test', timeout: 60 })).rejects.toThrow(ZodError);
    });

    test('should throw ZodError when exitCode is not a number', async () => {
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            stdout: 'output',
            stderr: '',
            exit_code: '0', // Invalid: should be number
          },
        },
        headers: {},
      });

      await expect(sandbox.execute({ command: 'test', timeout: 60 })).rejects.toThrow(ZodError);
    });

    test('should pass validation with valid CommandResponse', async () => {
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            stdout: 'valid output',
            stderr: '',
            exit_code: 0,
          },
        },
        headers: {},
      });

      const result = await sandbox.execute({ command: 'test', timeout: 60 });

      expect(result.stdout).toBe('valid output');
      expect(result.exitCode).toBe(0);
    });
  });

  describe('getStatus() - Zod validation', () => {
    beforeEach(async () => {
      // Start the sandbox first
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            host_name: 'test-host',
            host_ip: '127.0.0.1',
          },
        },
        headers: {},
      });
      mockGet.mockResolvedValue({
        data: {
          status: 'Success',
          result: { is_alive: true },
        },
        headers: {},
      });
      await sandbox.start();
    });

    test('should throw ZodError when isAlive is not a boolean', async () => {
      mockGet.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            is_alive: 'true', // Invalid: should be boolean
          },
        },
        headers: {},
      });

      await expect(sandbox.getStatus()).rejects.toThrow(ZodError);
    });

    test('should pass validation with valid SandboxStatusResponse', async () => {
      mockGet.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            is_alive: true,
            host_name: 'test-host',
          },
        },
        headers: {
          'x-rock-gateway-target-cluster': 'test-cluster',
        },
      });

      const result = await sandbox.getStatus();

      expect(result.sandboxId).toBe('test-id');
      expect(result.isAlive).toBe(true);
    });
  });

  describe('createSession() - Zod validation', () => {
    beforeEach(async () => {
      // Start the sandbox first
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            host_name: 'test-host',
            host_ip: '127.0.0.1',
          },
        },
        headers: {},
      });
      mockGet.mockResolvedValue({
        data: {
          status: 'Success',
          result: { is_alive: true },
        },
        headers: {},
      });
      await sandbox.start();
    });

    test('should throw ZodError when sessionType is invalid', async () => {
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            output: '',
            session_type: 'invalid', // Invalid: should be 'bash'
          },
        },
        headers: {},
      });

      await expect(sandbox.createSession({ 
        session: 'test', 
        startupSource: [], 
        envEnable: false 
      })).rejects.toThrow(ZodError);
    });

    test('should pass validation with valid CreateSessionResponse', async () => {
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            output: 'session created',
            session_type: 'bash',
          },
        },
        headers: {},
      });

      const result = await sandbox.createSession({ 
        session: 'test', 
        startupSource: [], 
        envEnable: false 
      });

      expect(result.output).toBe('session created');
      expect(result.sessionType).toBe('bash');
    });
  });

  describe('readFile() - Zod validation', () => {
    beforeEach(async () => {
      // Start the sandbox first
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            host_name: 'test-host',
            host_ip: '127.0.0.1',
          },
        },
        headers: {},
      });
      mockGet.mockResolvedValue({
        data: {
          status: 'Success',
          result: { is_alive: true },
        },
        headers: {},
      });
      await sandbox.start();
    });

    test('should throw ZodError when content is not a string', async () => {
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            content: { invalid: 'object' }, // Invalid: should be string
          },
        },
        headers: {},
      });

      await expect(sandbox.readFile({ path: '/test.txt' })).rejects.toThrow(ZodError);
    });

    test('should pass validation with valid ReadFileResponse', async () => {
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            content: 'file content',
          },
        },
        headers: {},
      });

      const result = await sandbox.readFile({ path: '/test.txt' });

      expect(result.content).toBe('file content');
    });
  });
});

/**
 * arun() session creation behavior tests
 * 
 * These tests verify that arun() matches Python SDK behavior:
 * - NORMAL mode: do NOT pre-create session, run command directly
 * - NOHUP mode: only create session when session is NOT provided
 */
describe('arun() - session creation behavior (matching Python SDK)', () => {
  let sandbox: Sandbox;
  let mockPost: jest.Mock;
  let mockGet: jest.Mock;

  beforeEach(() => {
    jest.clearAllMocks();
    mockPost = jest.fn();
    mockGet = jest.fn();
    mockedAxios.create = jest.fn().mockReturnValue({
      post: mockPost,
      get: mockGet,
    });

    sandbox = new Sandbox({
      image: 'test:latest',
      startupTimeout: 2,
    });
  });

  describe('NORMAL mode', () => {
    beforeEach(async () => {
      // Start the sandbox
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            host_name: 'test-host',
            host_ip: '127.0.0.1',
          },
        },
        headers: {},
      });
      mockGet.mockResolvedValue({
        data: {
          status: 'Success',
          result: { is_alive: true },
        },
        headers: {},
      });
      await sandbox.start();
    });

    test('should NOT call createSession before running command in NORMAL mode', async () => {
      // Mock run_in_session response
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            output: 'command output',
            exit_code: 0,
            failure_reason: '',
            expect_string: '',
          },
        },
        headers: {},
      });

      await sandbox.arun('echo hello', { mode: 'normal', session: 'existing-session' });

      // Should only have called run_in_session, NOT create_session
      const postCalls = mockPost.mock.calls;
      const calledEndpoints = postCalls.map((call: unknown[]) => call[0] as string);
      
      // Should NOT have called create_session
      expect(calledEndpoints.some((url: string) => url.includes('create_session'))).toBe(false);
      // Should have called run_in_session
      expect(calledEndpoints.some((url: string) => url.includes('run_in_session'))).toBe(true);
    });

    test('should directly run command without session pre-creation in NORMAL mode', async () => {
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            output: 'test output',
            exit_code: 0,
            failure_reason: '',
            expect_string: '',
          },
        },
        headers: {},
      });

      const result = await sandbox.arun('ls -la', { mode: 'normal', session: 'my-session' });

      expect(result.output).toBe('test output');
      expect(result.exitCode).toBe(0);
    });
  });

  describe('NOHUP mode', () => {
    beforeEach(async () => {
      // Start the sandbox
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            host_name: 'test-host',
            host_ip: '127.0.0.1',
          },
        },
        headers: {},
      });
      mockGet.mockResolvedValue({
        data: {
          status: 'Success',
          result: { is_alive: true },
        },
        headers: {},
      });
      await sandbox.start();
    });

    test('should NOT create session when session is provided in NOHUP mode', async () => {
      // Mock nohup command response (for PID extraction)
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            output: '__ROCK_PID_START__12345__ROCK_PID_END__',
            exit_code: 0,
            failure_reason: '',
            expect_string: '',
          },
        },
        headers: {},
      });

      // Mock kill -0 check (process completed)
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            output: '',
            exit_code: 1, // Process doesn't exist = completed
            failure_reason: '',
            expect_string: '',
          },
        },
        headers: {},
      });

      // Mock output file read
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            output: 'nohup output',
            exit_code: 0,
            failure_reason: '',
            expect_string: '',
          },
        },
        headers: {},
      });

      await sandbox.arun('long-running-command', { 
        mode: 'nohup', 
        session: 'existing-nohup-session',
        waitTimeout: 1,
        waitInterval: 1,
      });

      // Should NOT have called create_session since session was provided
      const postCalls = mockPost.mock.calls;
      const calledEndpoints = postCalls.map((call: unknown[]) => call[0] as string);
      
      expect(calledEndpoints.some((url: string) => url.includes('create_session'))).toBe(false);
    });

    test('should create session when session is NOT provided in NOHUP mode', async () => {
      // Mock create_session response
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            output: '',
            session_type: 'bash',
          },
        },
        headers: {},
      });

      // Mock nohup command response
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            output: '__ROCK_PID_START__12345__ROCK_PID_END__',
            exit_code: 0,
            failure_reason: '',
            expect_string: '',
          },
        },
        headers: {},
      });

      // Mock kill -0 check (process completed)
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            output: '',
            exit_code: 1,
            failure_reason: '',
            expect_string: '',
          },
        },
        headers: {},
      });

      // Mock output file read
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            output: 'nohup output',
            exit_code: 0,
            failure_reason: '',
            expect_string: '',
          },
        },
        headers: {},
      });

      await sandbox.arun('long-running-command', { 
        mode: 'nohup',
        // session NOT provided
        waitTimeout: 1,
        waitInterval: 1,
      });

      // Should have called create_session since session was NOT provided
      const postCalls = mockPost.mock.calls;
      const calledEndpoints = postCalls.map((call: unknown[]) => call[0] as string);
      
      expect(calledEndpoints.some((url: string) => url.includes('create_session'))).toBe(true);
    });
  });
});

/**
 * uploadByPath() async file I/O tests
 * 
 * These tests verify that uploadByPath uses async file operations (fs/promises)
 * instead of sync operations that block the event loop.
 */

// Mock fs/promises module
const mockAccess = jest.fn();
const mockReadFile = jest.fn();

jest.mock('fs/promises', () => ({
  access: mockAccess,
  readFile: mockReadFile,
}));

describe('uploadByPath() - async file I/O', () => {
  let sandbox: Sandbox;
  let mockPost: jest.Mock;
  let mockGet: jest.Mock;

  beforeEach(() => {
    jest.clearAllMocks();
    mockPost = jest.fn();
    mockGet = jest.fn();
    mockedAxios.create = jest.fn().mockReturnValue({
      post: mockPost,
      get: mockGet,
    });

    sandbox = new Sandbox({
      image: 'test:latest',
      startupTimeout: 2,
    });
  });

  describe('async file operations', () => {
    beforeEach(async () => {
      // Start the sandbox
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            sandbox_id: 'test-id',
            host_name: 'test-host',
            host_ip: '127.0.0.1',
          },
        },
        headers: {},
      });
      mockGet.mockResolvedValue({
        data: {
          status: 'Success',
          result: { is_alive: true },
        },
        headers: {},
      });
      await sandbox.start();
    });

    test('should use fs/promises.readFile (async) instead of fs.readFileSync', async () => {
      // Mock upload response
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {},
        },
        headers: {},
      });

      // Mock fs/promises methods
      mockAccess.mockResolvedValueOnce(undefined);
      mockReadFile.mockResolvedValueOnce(Buffer.from('test content'));

      const tempFile = '/tmp/test-upload-file.txt';
      const result = await sandbox.uploadByPath(tempFile, '/remote/path.txt');

      // Verify async readFile was called instead of sync readFileSync
      expect(mockReadFile).toHaveBeenCalledWith(tempFile);
      expect(result.success).toBe(true);
    });

    test('should use fs/promises.access (async) for file existence check', async () => {
      // Mock upload response
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {},
        },
        headers: {},
      });

      // Mock fs/promises methods
      mockAccess.mockResolvedValueOnce(undefined);
      mockReadFile.mockResolvedValueOnce(Buffer.from('test content'));

      const tempFile = '/tmp/test-upload-file.txt';
      await sandbox.uploadByPath(tempFile, '/remote/path.txt');

      // Verify async access was called instead of sync existsSync
      expect(mockAccess).toHaveBeenCalledWith(tempFile);
    });

    test('should return failure when file does not exist (async access throws)', async () => {
      // Mock access to throw (file not found)
      mockAccess.mockRejectedValueOnce(new Error('ENOENT'));

      const result = await sandbox.uploadByPath('/nonexistent/file.txt', '/remote/path.txt');

      expect(result.success).toBe(false);
      expect(result.message).toContain('File not found');
    });
  });
});