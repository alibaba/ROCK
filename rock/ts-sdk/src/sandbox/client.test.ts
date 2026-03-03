/**
 * Tests for Sandbox Client - Exception handling
 */

import axios from 'axios';
import { Sandbox } from './client.js';
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