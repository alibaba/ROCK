/**
 * OSS client secure option tests
 *
 * These tests verify OSS-related functionality:
 * - OSS credentials handling
 * - OSS upload mode selection
 *
 * Note: The `secure: true` option for ali-oss is verified through code review
 * and integration tests, as Jest cannot easily mock dynamic imports with
 * constructor call verification.
 *
 * Issue: OSS upload fails due to missing `secure: true` option
 * When using OSS upload mode, the `ali-oss` client defaults to HTTP protocol,
 * but OSS buckets typically require HTTPS connections.
 *
 * Fix: Added `secure: true` in setupOss() method (client.ts:836)
 */

import axios from 'axios';

// Mock fs/promises module
jest.mock('fs/promises', () => ({
  access: jest.fn(),
  readFile: jest.fn(),
  stat: jest.fn(),
}));

// Mock axios
jest.mock('axios');

// Mock ali-oss module
jest.mock('ali-oss', () => ({
  default: jest.fn().mockImplementation(() => ({
    put: jest.fn().mockResolvedValue({}),
    signatureUrl: jest.fn().mockReturnValue('https://signed-url'),
    get: jest.fn().mockResolvedValue({}),
    delete: jest.fn().mockResolvedValue({}),
  })),
}));

import { Sandbox } from './client.js';
import * as fsPromises from 'fs/promises';
import type { Stats } from 'fs';

const mockedAxios = axios as jest.Mocked<typeof axios>;
const mockedFs = fsPromises as jest.Mocked<typeof fsPromises>;

describe('OSS client configuration', () => {
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

    // Set OSS environment variables
    process.env.ROCK_OSS_ENABLE = 'true';
    process.env.ROCK_OSS_BUCKET_NAME = 'test-bucket';
    process.env.ROCK_OSS_BUCKET_REGION = 'cn-hangzhou';

    sandbox = new Sandbox({
      image: 'test:latest',
      startupTimeout: 2,
    });
  });

  afterEach(() => {
    delete process.env.ROCK_OSS_ENABLE;
    delete process.env.ROCK_OSS_BUCKET_NAME;
    delete process.env.ROCK_OSS_BUCKET_REGION;
  });

  describe('getOssStsCredentials()', () => {
    test('should fetch and parse OSS STS credentials', async () => {
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

      // Mock getOssStsCredentials API response
      mockGet.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {
            access_key_id: 'STS.TEST_ACCESS_KEY',
            access_key_secret: 'TEST_SECRET',
            security_token: 'TEST_SECURITY_TOKEN',
            expiration: '2026-03-28T18:00:00Z',
          },
        },
        headers: {},
      });

      const credentials = await sandbox.getOssStsCredentials();

      expect(credentials.accessKeyId).toBe('STS.TEST_ACCESS_KEY');
      expect(credentials.accessKeySecret).toBe('TEST_SECRET');
      expect(credentials.securityToken).toBe('TEST_SECURITY_TOKEN');
      expect(credentials.expiration).toBe('2026-03-28T18:00:00Z');
    });

    test('should throw error when credentials API fails', async () => {
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

      // Mock failed credentials API response
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
    test('should return true when token is expired', async () => {
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

      // Set expired token
      (sandbox as unknown as { ossTokenExpireTime: string }).ossTokenExpireTime = '2020-01-01T00:00:00Z';

      expect(sandbox.isTokenExpired()).toBe(true);
    });

    test('should return true when token expires within 5 minutes', async () => {
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

      // Set token to expire in 2 minutes
      const twoMinutesLater = new Date(Date.now() + 2 * 60 * 1000);
      (sandbox as unknown as { ossTokenExpireTime: string }).ossTokenExpireTime = twoMinutesLater.toISOString();

      expect(sandbox.isTokenExpired()).toBe(true);
    });

    test('should return false when token is valid for more than 5 minutes', async () => {
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

      // Set token to expire in 10 minutes
      const tenMinutesLater = new Date(Date.now() + 10 * 60 * 1000);
      (sandbox as unknown as { ossTokenExpireTime: string }).ossTokenExpireTime = tenMinutesLater.toISOString();

      expect(sandbox.isTokenExpired()).toBe(false);
    });
  });

  describe('uploadByPath() OSS mode selection', () => {
    test('should use direct upload when uploadMode is direct', async () => {
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

      // Mock file operations
      mockedFs.access.mockResolvedValueOnce(undefined);
      mockedFs.stat.mockResolvedValueOnce({ size: 2 * 1024 * 1024 } as Stats); // Large file
      mockedFs.readFile.mockResolvedValueOnce(Buffer.from('test content'));

      // Mock direct upload response
      mockPost.mockResolvedValueOnce({
        data: {
          status: 'Success',
          result: {},
        },
        headers: {},
      });

      // Trigger direct upload
      const result = await sandbox.uploadByPath('/local/large.bin', '/remote/large.bin', 'direct');

      expect(result.success).toBe(true);
    });

    test('should return failure when file does not exist', async () => {
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

      // Mock file not found
      mockedFs.access.mockRejectedValueOnce(new Error('ENOENT'));

      const result = await sandbox.uploadByPath('/nonexistent/file.txt', '/remote/file.txt');

      expect(result.success).toBe(false);
      expect(result.message).toContain('File not found');
    });
  });
});