/**
 * HTTP utilities tests - case conversion integration
 */

import axios, { AxiosError, AxiosResponse } from 'axios';
import https from 'https';
import { HttpUtils, HttpResponse, sharedHttpsAgent } from './http.js';

// Save the real AxiosError class before mocking axios
// This is necessary for instanceof checks in HTTP error tests
const RealAxiosError = AxiosError;

// Mock axios
jest.mock('axios');
const mockedAxios = axios as jest.Mocked<typeof axios>;

/**
 * Helper to create a proper AxiosResponse for tests
 */
function createMockResponse(status: number, statusText: string, data: unknown = {}): AxiosResponse {
  return {
    status,
    statusText,
    headers: {},
    data,
    config: {} as any,
    request: {} as any,
  };
}

/**
 * Helper to create an AxiosError with response property properly set
 * (needed for Jest environment where constructor doesn't set response)
 */
function createAxiosError(
  message: string,
  code: string,
  status: number,
  statusText: string,
  data: unknown = {}
): AxiosError {
  const error = new RealAxiosError(
    message,
    code,
    { headers: {} } as any,
    { url: 'http://test/api', method: 'POST' } as any
  );
  // Manually set response property (axios does this internally)
  (error as any).response = createMockResponse(status, statusText, data);
  return error;
}

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

describe('HttpUtils shared https.Agent', () => {
  test('exports a shared https.Agent for connection pooling', () => {
    // Following Python SDK pattern: _SHARED_SSL_CONTEXT
    // The agent should be created once at module level
    expect(sharedHttpsAgent).toBeDefined();
    expect(sharedHttpsAgent).toBeInstanceOf(https.Agent);
  });

  test('shared agent has keepAlive enabled for connection reuse', () => {
    // keepAlive enables TCP keep-alive sockets for better performance
    // Access through options property as per Node.js Agent implementation
    expect((sharedHttpsAgent as unknown as { keepAlive: boolean }).keepAlive).toBe(true);
  });

  test('shared agent has rejectUnauthorized enabled for security', () => {
    expect((sharedHttpsAgent.options as { rejectUnauthorized?: boolean }).rejectUnauthorized).toBe(true);
  });

  test('multiple requests use the same agent instance', async () => {
    const mockResponse = {
      data: { status: 'Success', result: { sandbox_id: '123' } },
      headers: {},
    };
    const mockPost = jest.fn().mockResolvedValue(mockResponse);
    const mockGet = jest.fn().mockResolvedValue(mockResponse);
    
    const capturedAgents: https.Agent[] = [];
    mockedAxios.create = jest.fn().mockImplementation((config) => {
      if (config?.httpsAgent) {
        capturedAgents.push(config.httpsAgent);
      }
      return { post: mockPost, get: mockGet };
    });

    // Make multiple requests
    await HttpUtils.post('http://test/api1', {}, {});
    await HttpUtils.post('http://test/api2', {}, {});
    await HttpUtils.get('http://test/api3', {});

    // All requests should use the same agent instance (same reference)
    expect(capturedAgents.length).toBe(3);
    expect(capturedAgents[0]).toBe(sharedHttpsAgent);
    expect(capturedAgents[1]).toBe(sharedHttpsAgent);
    expect(capturedAgents[2]).toBe(sharedHttpsAgent);
    
    // All captured agents should be the exact same object reference
    expect(capturedAgents[0]).toBe(capturedAgents[1]);
    expect(capturedAgents[1]).toBe(capturedAgents[2]);
  });
});

describe('HttpUtils HTTP error handling', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe('post', () => {
    test('preserves response property in HTTP errors (e.g., 401 Unauthorized)', async () => {
      const axiosError = createAxiosError(
        'Request failed with status code 401',
        'ERR_BAD_REQUEST',
        401,
        'Unauthorized',
        { error: 'Invalid API key' }
      );

      const mockPost = jest.fn().mockRejectedValue(axiosError);
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      const error = await HttpUtils.post('http://test/api', {}, {}).catch(e => e);

      // Verify: error should be AxiosError with response property preserved
      expect(error).toBeInstanceOf(RealAxiosError);
      expect(error.response).toBeDefined();
      expect(error.response.status).toBe(401);
      expect(error.response.statusText).toBe('Unauthorized');
    });

    test('preserves response property for 403 Forbidden', async () => {
      const axiosError = createAxiosError(
        'Request failed with status code 403',
        'ERR_BAD_REQUEST',
        403,
        'Forbidden',
        { error: 'Permission denied' }
      );

      const mockPost = jest.fn().mockRejectedValue(axiosError);
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      const error = await HttpUtils.post('http://test/api', {}, {}).catch(e => e);

      expect(error).toBeInstanceOf(RealAxiosError);
      expect(error.response.status).toBe(403);
    });

    test('preserves response property for 500 Internal Server Error', async () => {
      const axiosError = createAxiosError(
        'Request failed with status code 500',
        'ERR_BAD_RESPONSE',
        500,
        'Internal Server Error',
        { error: 'Database connection failed' }
      );

      const mockPost = jest.fn().mockRejectedValue(axiosError);
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      const error = await HttpUtils.post('http://test/api', {}, {}).catch(e => e);

      expect(error).toBeInstanceOf(RealAxiosError);
      expect(error.response.status).toBe(500);
    });
  });

  describe('get', () => {
    test('preserves response property in HTTP errors', async () => {
      const axiosError = createAxiosError(
        'Request failed with status code 404',
        'ERR_BAD_REQUEST',
        404,
        'Not Found',
        { error: 'Resource not found' }
      );

      const mockGet = jest.fn().mockRejectedValue(axiosError);
      mockedAxios.create = jest.fn().mockReturnValue({ get: mockGet });

      const error = await HttpUtils.get('http://test/api', {}).catch(e => e);

      expect(error).toBeInstanceOf(RealAxiosError);
      expect(error.response.status).toBe(404);
    });
  });

  describe('postMultipart', () => {
    test('preserves response property in HTTP errors', async () => {
      const axiosError = createAxiosError(
        'Request failed with status code 413',
        'ERR_BAD_REQUEST',
        413,
        'Payload Too Large',
        { error: 'File size exceeds limit' }
      );

      const mockPost = jest.fn().mockRejectedValue(axiosError);
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      const error = await HttpUtils.postMultipart('http://test/upload', {}, {}, {}).catch(e => e);

      expect(error).toBeInstanceOf(RealAxiosError);
      expect(error.response.status).toBe(413);
    });
  });
});