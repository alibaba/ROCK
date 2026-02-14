/**
 * HTTP utilities tests - case conversion integration
 */

import axios from 'axios';
import { HttpUtils } from './http.js';

// Mock axios
jest.mock('axios');
const mockedAxios = axios as jest.Mocked<typeof axios>;

describe('HttpUtils case conversion', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  describe('post', () => {
    test('converts request body from camelCase to snake_case', async () => {
      const mockPost = jest.fn().mockResolvedValue({
        data: { status: 'Success', result: { sandbox_id: '123' } },
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
      });
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      interface TestResponse {
        sandboxId: string;
        hostName: string;
        isAlive: boolean;
      }

      const result = await HttpUtils.post<{ status: string; result: TestResponse }>(
        'http://test/api',
        {},
        { sandboxId: 'test-id' }
      );

      // Verify response was converted to camelCase
      expect(result.result).toEqual({
        sandboxId: '123',
        hostName: 'localhost',
        isAlive: true,
      });
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
      });
      mockedAxios.create = jest.fn().mockReturnValue({ post: mockPost });

      interface TestResult {
        sandboxId: string;
        portMapping: {
          httpPort: number;
          httpsPort: number;
        };
      }

      const result = await HttpUtils.post<{ status: string; result: TestResult }>(
        'http://test/api',
        {},
        {}
      );

      expect(result.result.portMapping).toEqual({
        httpPort: 8080,
        httpsPort: 8443,
      });
    });
  });

  describe('get', () => {
    test('converts response from snake_case to camelCase', async () => {
      const mockGet = jest.fn().mockResolvedValue({
        data: {
          sandbox_id: '123',
          is_alive: true,
          host_name: 'localhost',
        },
      });
      mockedAxios.create = jest.fn().mockReturnValue({ get: mockGet });

      interface TestResponse {
        sandboxId: string;
        isAlive: boolean;
        hostName: string;
      }

      const result = await HttpUtils.get<TestResponse>('http://test/api', {});

      expect(result).toEqual({
        sandboxId: '123',
        isAlive: true,
        hostName: 'localhost',
      });
    });
  });
});
