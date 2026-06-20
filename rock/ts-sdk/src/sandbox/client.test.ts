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
      // Mock the start_async API to return an error response with code on envelope
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Failed',
          code: Codes.BAD_REQUEST,
          result: {
            sandbox_id: 'test-id',
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
          code: Codes.INTERNAL_SERVER_ERROR,
          result: {
            sandbox_id: 'test-id',
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
          code: Codes.COMMAND_ERROR,
          result: {
            sandbox_id: 'test-id',
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
          code: 7000,
          result: {
            sandbox_id: 'test-id',
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
          code: Codes.BAD_REQUEST,
          result: {},
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
          code: Codes.BAD_REQUEST,
          result: {},
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

    test('should wrap multi-line scripts with bash -c in nohup mode', async () => {
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
            output: 'script executed',
            exit_code: 0,
            failure_reason: '',
            expect_string: '',
          },
        },
        headers: {},
      });

      // Multi-line script
      const multiLineScript = `#!/bin/bash
set -e
echo "line 1"
echo "line 2"
`;

      await sandbox.arun(multiLineScript, { 
        mode: 'nohup', 
        session: 'test-session',
        waitTimeout: 1,
        waitInterval: 1,
      });

      // Verify the command sent to run_in_session wraps multi-line script with bash -c
      // mockPost.calls[x][0] = URL, mockPost.calls[x][1] = data object
      const postCalls = mockPost.mock.calls;
      
      // Find the call with nohup command
      let commandArg: string | undefined;
      for (const call of postCalls) {
        // call[1] is the data object passed to post
        const data = call[1] as unknown;
        if (data && typeof data === 'object' && 'command' in data) {
          const cmd = (data as { command: string }).command;
          if (cmd.includes('nohup')) {
            commandArg = cmd;
            break;
          }
        }
      }
      
      expect(commandArg).toBeDefined();
      
      // Should wrap with bash -c for multi-line scripts
      expect(commandArg).toMatch(/nohup bash -c/);
      // Should NOT have raw multi-line content directly after nohup
      expect(commandArg).not.toMatch(/nohup #!\/bin\/bash/);
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
const mockStat = jest.fn();

jest.mock('fs/promises', () => ({
  access: mockAccess,
  readFile: mockReadFile,
  stat: mockStat,
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
      mockStat.mockResolvedValueOnce({ size: 1024 }); // Small file, direct upload
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
      mockStat.mockResolvedValueOnce({ size: 1024 }); // Small file
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

/**
 * OSS STS Credentials tests
 * 
 * These tests verify OSS credential management:
 * - getOssStsCredentials: Fetching STS token from /get_token API
 * - isTokenExpired: Checking token expiration with 5-minute buffer
 */
describe('OSS STS Credentials', () => {
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

  describe('getOssStsCredentials()', () => {
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

    test('should fetch STS credentials from /get_token API', async () => {
      // Mock get_token response - API returns snake_case which gets converted to camelCase
      mockGet.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            access_key_id: 'STS.NUxxxxxxxxxxxxxx',
            access_key_secret: 'test-secret',
            security_token: 'CAISxxxxxxxxxxxxxxxx',
            expiration: '2025-03-28T13:00:00Z',
          },
        },
        headers: {},
      });

      const credentials = await sandbox.getOssStsCredentials();

      expect(credentials.accessKeyId).toBe('STS.NUxxxxxxxxxxxxxx');
      expect(credentials.accessKeySecret).toBe('test-secret');
      expect(credentials.securityToken).toBe('CAISxxxxxxxxxxxxxxxx');
      expect(credentials.expiration).toBe('2025-03-28T13:00:00Z');
    });

    test('should throw error when API returns failure', async () => {
      mockGet.mockResolvedValueOnce({
        data: {
          status: 'Failed',
          message: 'Token generation failed',
        },
        headers: {},
      });

      await expect(sandbox.getOssStsCredentials()).rejects.toThrow();
    });
  });

  describe('isTokenExpired()', () => {
    test('should return true when token is expired', () => {
      // Set expired time in the past
      (sandbox as unknown as Record<string, string>).ossTokenExpireTime = '2020-01-01T00:00:00Z';
      
      expect(sandbox.isTokenExpired()).toBe(true);
    });

    test('should return true when token expires within 5 minutes', () => {
      // Set expiration 2 minutes in the future
      const twoMinutesLater = new Date(Date.now() + 2 * 60 * 1000);
      const expiration = twoMinutesLater.toISOString();
      
      (sandbox as unknown as Record<string, string>).ossTokenExpireTime = expiration;
      
      expect(sandbox.isTokenExpired()).toBe(true);
    });

    test('should return false when token is valid for more than 5 minutes', () => {
      // Set expiration 10 minutes in the future
      const tenMinutesLater = new Date(Date.now() + 10 * 60 * 1000);
      const expiration = tenMinutesLater.toISOString();
      
      (sandbox as unknown as Record<string, string>).ossTokenExpireTime = expiration;
      
      expect(sandbox.isTokenExpired()).toBe(false);
    });

    test('should return true when expiration time is not set', () => {
      (sandbox as unknown as Record<string, string>).ossTokenExpireTime = '';
      
      expect(sandbox.isTokenExpired()).toBe(true);
    });
  });
});

/**
 * downloadFile() tests
 * 
 * These tests verify file download via OSS:
 * - OSS enable check
 * - Remote file validation
 * - ossutil installation
 * - OSS upload from sandbox
 * - Local download from OSS
 */
describe('downloadFile()', () => {
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

  test('should return failure when remote path is empty', async () => {
    const result = await sandbox.downloadFile('', '/local/file.txt');

    expect(result.success).toBe(false);
    expect(result.message).toContain('Remote path is required');
  });

  test('should accept downloadMode parameter', async () => {
    // Mock execute for file existence check and size check
    mockPost.mockResolvedValueOnce({
      data: {
        status: 'Success',
        result: {
          stdout: 'File content',
          stderr: '',
          exit_code: 0,
        },
      },
      headers: {},
    }).mockResolvedValueOnce({
      data: {
        status: 'Success',
        result: {
          stdout: '1024',
          stderr: '',
          exit_code: 0,
        },
      },
      headers: {},
    });

    // Should compile and accept downloadMode parameter
    const result = await sandbox.downloadFile('/remote/file.txt', '/local/file.txt', { downloadMode: 'direct' });

    expect(result).toBeDefined();
  });

  test('should accept uploadMode parameter', async () => {
    // Mock direct upload response
    mockPost.mockResolvedValueOnce({
      data: {
        status: 'Success',
        result: {},
      },
      headers: {},
    });

    mockAccess.mockResolvedValueOnce(undefined);
    mockStat.mockResolvedValueOnce({ size: 1024 });
    mockReadFile.mockResolvedValueOnce(Buffer.from('test content'));

    // Should compile and accept uploadMode parameter
    const result = await sandbox.uploadByPath('/local/file.txt', '/remote/file.txt', { uploadMode: 'direct' });

    expect(result).toBeDefined();
  });
});

/**
 * uploadByPath() with uploadMode tests
 * 
 * These tests verify upload mode selection:
 * - AUTO: Choose based on file size and OSS availability
 * - DIRECT: Force direct HTTP upload
 * - OSS: Force OSS upload
 */
describe('uploadByPath() with uploadMode', () => {
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

  test('should accept uploadMode parameter', async () => {
    // Mock direct upload response
    mockPost.mockResolvedValueOnce({
      data: {
        status: 'Success',
        result: {},
      },
      headers: {},
    });

    mockAccess.mockResolvedValueOnce(undefined);
    mockReadFile.mockResolvedValueOnce(Buffer.from('test content'));

    // Should compile and accept uploadMode parameter
    const result = await sandbox.uploadByPath('/local/file.txt', '/remote/file.txt', { uploadMode: 'direct' });

    expect(result).toBeDefined();
  });
});