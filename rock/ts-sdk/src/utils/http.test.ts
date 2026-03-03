/**
 * HTTP utilities tests - case conversion integration
 */

import axios from 'axios';
import { HttpUtils, HttpResponse } from './http.js';

// Mock axios
jest.mock('axios');
const mockedAxios = axios as jest.Mocked<typeof axios>;

// Mock FormData for Node.js environment
class MockFormData {
  private entries: [string, string | Blob][] = [];

  append(key: string, value: string | Blob, filename?: string): void {
    this.entries.push([key, value]);
  }

  getEntries(): [string, string | Blob][] {
    return this.entries;
  }
}

// @ts-expect-error - Mocking global FormData
global.FormData = MockFormData;

// Mock Blob for Node.js environment
class MockBlob {
  private content: Buffer;
  private type: string;

  constructor(parts: Buffer[], options?: { type?: string }) {
    this.content = Buffer.concat(parts);
    this.type = options?.type ?? '';
  }
}

// Blob may already exist in Node.js 18+, so we use type assertion
(global as unknown as { Blob: typeof MockBlob }).Blob = MockBlob;

describe('HttpUtils case conversion', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe('post', () => {
    test('converts request body from camelCase to snake_case', async () => {
      const mockPost = jest.fn().mockResolvedValue({
        data: { status: 'Success', result: { sandbox_id: '123' } },
        headers: { 'x-request-id': 'test-request-id' },
      });
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      // Send camelCase request
      await HttpUtils.post(
        'http://test/api',
        {},
        { sandboxId: 'test-id', isAlive: true }
      );

      // Verify request was converted to snake_case
      expect(mockPost).toHaveBeenCalledWith(
        'http://test/api',
        expect.objectContaining({
          sandbox_id: 'test-id',
          is_alive: true,
        })
      );
    });

    test('converts response from snake_case to camelCase', async () => {
      const mockPost = jest.fn().mockResolvedValue({
        data: {
          status: 'Success',
          result: {
            sandbox_id: '123',
            host_name: 'localhost',
            is_alive: true,
          },
        },
        headers: { 'x-request-id': 'test-request-id' },
      });
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      interface TestResponse {
        sandboxId: string;
        hostName: string;
        isAlive: boolean;
      }

      const result = await HttpUtils.post<TestResponse>(
        'http://test/api',
        {},
        { sandboxId: 'test-id' }
      );

      // Verify response was converted to camelCase
      expect(result.status).toBe('Success');
      expect(result.result).toEqual({
        sandboxId: '123',
        hostName: 'localhost',
        isAlive: true,
      });
      expect(result.headers).toHaveProperty('x-request-id');
    });

    test('handles nested objects in response', async () => {
      const mockPost = jest.fn().mockResolvedValue({
        data: {
          status: 'Success',
          result: {
            sandbox_id: '123',
            port_mapping: {
              http_port: 8080,
              https_port: 8443,
            },
          },
        },
        headers: {},
      });
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      interface TestResult {
        sandboxId: string;
        portMapping: {
          httpPort: number;
          httpsPort: number;
        };
      }

      const result = await HttpUtils.post<TestResult>(
        'http://test/api',
        {},
        {}
      );

      expect(result.result!.portMapping).toEqual({
        httpPort: 8080,
        httpsPort: 8443,
      });
    });
  });

  describe('get', () => {
    test('converts response from snake_case to camelCase', async () => {
      const mockGet = jest.fn().mockResolvedValue({
        data: {
          status: 'Success',
          result: {
            sandbox_id: '123',
            is_alive: true,
            host_name: 'localhost',
          },
        },
        headers: { 'x-request-id': 'test-request-id' },
      });
      mockedAxios.create = jest.fn().mockReturnValue({ get: mockGet });

      interface TestResponse {
        sandboxId: string;
        isAlive: boolean;
        hostName: string;
      }

      const result = await HttpUtils.get<TestResponse>('http://test/api', {});

      expect(result.status).toBe('Success');
      expect(result.result).toEqual({
        sandboxId: '123',
        isAlive: true,
        hostName: 'localhost',
      });
      expect(result.headers).toHaveProperty('x-request-id');
    });
  });

  describe('postMultipart', () => {
    test('converts form data keys from camelCase to snake_case', async () => {
      const mockPost = jest.fn().mockResolvedValue({
        data: { status: 'Success', result: null },
        headers: {},
      });
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      await HttpUtils.postMultipart(
        'http://test/upload',
        {},
        { targetPath: '/tmp/test', sandboxId: '123' },
        {}
      );

      // Verify FormData was created with snake_case keys
      const formData = mockPost.mock.calls[0][1] as MockFormData;
      const entries = formData.getEntries();
      
      const keys = entries.map(([key]) => key);
      expect(keys).toContain('target_path');
      expect(keys).toContain('sandbox_id');
    });

    test('sets Content-Type to null to let axios auto-detect FormData', async () => {
      // This test verifies the fix for: manually setting Content-Type: multipart/form-data
      // prevents axios from adding the required boundary parameter.
      // The correct behavior is to set Content-Type to null (removing default 'application/json')
      // so axios can auto-detect FormData and set Content-Type: multipart/form-data; boundary=xxx

      let capturedHeaders: Record<string, string | null> | undefined;
      let capturedConfig: { headers?: Record<string, string | null> } | undefined;
      const mockPost = jest.fn().mockImplementation((_url, _data, config) => {
        capturedConfig = config;
        return Promise.resolve({
          data: { status: 'Success', result: null },
          headers: {},
        });
      });
      const mockCreate = jest.fn().mockImplementation((config) => {
        // Capture the headers passed to axios.create
        capturedHeaders = config?.headers;
        return {
          post: mockPost,
          defaults: { headers: { 'Content-Type': 'application/json' } },
        };
      });
      mockedAxios.create = mockCreate;

      await HttpUtils.postMultipart(
        'http://test/upload',
        { Authorization: 'Bearer token' },
        { sandboxId: '123' },
        {}
      );

      // CRITICAL: Content-Type should be set to null in the post config
      // This removes the default 'application/json' and allows axios to auto-detect FormData
      // and set the correct Content-Type with boundary
      expect(capturedConfig).toBeDefined();
      expect(capturedConfig?.headers).toHaveProperty('Content-Type', null);
      
      // Headers passed to createClient should preserve other headers
      expect(capturedHeaders).toHaveProperty('Authorization', 'Bearer token');
    });

    test('adds files to FormData with snake_case field names', async () => {
      const mockPost = jest.fn().mockResolvedValue({
        data: { status: 'Success', result: null },
        headers: {},
      });
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      const fileContent = Buffer.from('test file content');
      await HttpUtils.postMultipart(
        'http://test/upload',
        {},
        {},
        { myFile: ['test.txt', fileContent, 'text/plain'] }
      );

      const formData = mockPost.mock.calls[0][1] as MockFormData;
      const entries = formData.getEntries();
      
      // Field name should be converted to snake_case
      const fileEntry = entries.find(([key]) => key === 'my_file');
      expect(fileEntry).toBeDefined();
    });

    test('handles Buffer files correctly', async () => {
      const mockPost = jest.fn().mockResolvedValue({
        data: { status: 'Success', result: null },
        headers: {},
      });
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      const fileBuffer = Buffer.from('binary data');
      await HttpUtils.postMultipart(
        'http://test/upload',
        {},
        {},
        { file: fileBuffer }
      );

      const formData = mockPost.mock.calls[0][1] as MockFormData;
      const entries = formData.getEntries();
      
      expect(entries.length).toBe(1);
      expect(entries[0]?.[0]).toBe('file');
    });
  });
});