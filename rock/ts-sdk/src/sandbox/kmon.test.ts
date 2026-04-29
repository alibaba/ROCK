/**
 * KmonHostIpResolver unit tests
 */

import axios from 'axios';

// Mock axios
jest.mock('axios');
const mockedAxios = axios as jest.Mocked<typeof axios>;

// Import after mocking
import { KmonHostIpResolver } from './kmon.js';

describe('KmonHostIpResolver', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    jest.clearAllMocks();
    // Reset environment variables
    process.env = { ...originalEnv };
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  describe('constructor', () => {
    test('should create instance with token from parameter', () => {
      const resolver = new KmonHostIpResolver({ token: 'test-token' });
      expect(resolver).toBeInstanceOf(KmonHostIpResolver);
    });

    test('should create instance with token from environment variable', () => {
      process.env.ROCK_KMON_TOKEN = 'env-token';
      const resolver = new KmonHostIpResolver();
      expect(resolver).toBeInstanceOf(KmonHostIpResolver);
    });

    test('should throw error when token is not provided', () => {
      delete process.env.ROCK_KMON_TOKEN;
      expect(() => new KmonHostIpResolver()).toThrow('ROCK_KMON_TOKEN is required');
    });

    test('should use default values when not provided', () => {
      const resolver = new KmonHostIpResolver({ token: 'test-token' });
      // Access private config through reflection for testing
      const config = (resolver as unknown as { config: Required<import('./kmon.js').KmonConfig> }).config;
      
      expect(config.baseUrl).toBe('https://kmon-metric.alibaba-inc.com');
      expect(config.tenants).toEqual(['default', 'gen_ai']);
      expect(config.maxQueryDays).toBe(7);
      expect(config.maxQueryRangeMs).toBe(2 * 24 * 60 * 60 * 1000); // 2 days
    });

    test('should use custom baseUrl from parameter', () => {
      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        baseUrl: 'https://custom.kmon.com',
      });
      const config = (resolver as unknown as { config: Required<import('./kmon.js').KmonConfig> }).config;
      expect(config.baseUrl).toBe('https://custom.kmon.com');
    });

    test('should use custom baseUrl from environment variable', () => {
      process.env.ROCK_KMON_TOKEN = 'env-token';
      process.env.ROCK_KMON_BASE_URL = 'https://env.kmon.com';
      const resolver = new KmonHostIpResolver();
      const config = (resolver as unknown as { config: Required<import('./kmon.js').KmonConfig> }).config;
      expect(config.baseUrl).toBe('https://env.kmon.com');
    });

    test('should use custom tenants from parameter', () => {
      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        tenants: ['tenant1', 'tenant2'],
      });
      const config = (resolver as unknown as { config: Required<import('./kmon.js').KmonConfig> }).config;
      expect(config.tenants).toEqual(['tenant1', 'tenant2']);
    });

    test('should use custom tenants from environment variable', () => {
      process.env.ROCK_KMON_TOKEN = 'env-token';
      process.env.ROCK_KMON_TENANTS = 'env_tenant1,env_tenant2';
      const resolver = new KmonHostIpResolver();
      const config = (resolver as unknown as { config: Required<import('./kmon.js').KmonConfig> }).config;
      expect(config.tenants).toEqual(['env_tenant1', 'env_tenant2']);
    });

    test('should use custom maxQueryDays', () => {
      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        maxQueryDays: 14,
      });
      const config = (resolver as unknown as { config: Required<import('./kmon.js').KmonConfig> }).config;
      expect(config.maxQueryDays).toBe(14);
    });

    test('should use custom maxQueryRangeMs', () => {
      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        maxQueryRangeMs: 24 * 60 * 60 * 1000, // 1 day
      });
      const config = (resolver as unknown as { config: Required<import('./kmon.js').KmonConfig> }).config;
      expect(config.maxQueryRangeMs).toBe(24 * 60 * 60 * 1000);
    });

    test('parameter should override environment variable', () => {
      process.env.ROCK_KMON_TOKEN = 'env-token';
      process.env.ROCK_KMON_BASE_URL = 'https://env.kmon.com';
      
      const resolver = new KmonHostIpResolver({
        token: 'param-token',
        baseUrl: 'https://param.kmon.com',
      });
      const config = (resolver as unknown as { config: Required<import('./kmon.js').KmonConfig> }).config;
      
      expect(config.token).toBe('param-token');
      expect(config.baseUrl).toBe('https://param.kmon.com');
    });
  });

  describe('resolve', () => {
    test('should return hostIp when found in default tenant', async () => {
      // Response is an array directly (not { results: [...] })
      mockedAxios.post.mockResolvedValueOnce({
        data: [{
          tags: { ip: '192.168.1.100' },
        }],
      });

      const resolver = new KmonHostIpResolver({ token: 'test-token' });
      const hostIp = await resolver.resolve('sandbox-123');
      
      expect(hostIp).toBe('192.168.1.100');
      expect(mockedAxios.post).toHaveBeenCalledTimes(1);
      expect(mockedAxios.post).toHaveBeenCalledWith(
        expect.stringContaining('tenant=default'),
        expect.objectContaining({
          queries: expect.arrayContaining([
            expect.objectContaining({
              tags: { ip: '*', sandbox_id: 'sandbox-123' },
            }),
          ]),
        }),
        expect.any(Object)
      );
    });

    test('should fallback to gen_ai tenant when not found in default', async () => {
      // First call (default tenant) returns empty array
      mockedAxios.post.mockResolvedValueOnce({
        data: [],
      });
      // Second call (gen_ai tenant) returns result
      mockedAxios.post.mockResolvedValueOnce({
        data: [{
          tags: { ip: '10.0.0.50' },
        }],
      });

      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        maxQueryDays: 1, // Reduce to minimize API calls
        maxQueryRangeMs: 24 * 60 * 60 * 1000, // 1 day = 1 segment
      });
      const hostIp = await resolver.resolve('sandbox-456');
      
      expect(hostIp).toBe('10.0.0.50');
      // Should have called both tenants
      expect(mockedAxios.post).toHaveBeenCalledWith(
        expect.stringContaining('tenant=default'),
        expect.any(Object),
        expect.any(Object)
      );
      expect(mockedAxios.post).toHaveBeenCalledWith(
        expect.stringContaining('tenant=gen_ai'),
        expect.any(Object),
        expect.any(Object)
      );
    });

    test('should throw error when hostIp not found in any tenant', async () => {
      // All tenants return empty array
      mockedAxios.post.mockResolvedValue({
        data: [],
      });

      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        maxQueryDays: 1,
        maxQueryRangeMs: 24 * 60 * 60 * 1000,
      });
      
      await expect(resolver.resolve('sandbox-not-found')).rejects.toThrow(
        'Cannot find hostIp for sandbox sandbox-not-found in any tenant'
      );
    });

    test('should handle API error gracefully and continue to next tenant', async () => {
      // First tenant throws error
      mockedAxios.post.mockRejectedValueOnce(new Error('Network error'));
      // Second tenant returns result
      mockedAxios.post.mockResolvedValueOnce({
        data: [{
          tags: { ip: '172.16.0.1' },
        }],
      });

      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        maxQueryDays: 1,
        maxQueryRangeMs: 24 * 60 * 60 * 1000,
      });
      const hostIp = await resolver.resolve('sandbox-789');
      
      expect(hostIp).toBe('172.16.0.1');
    });

    test('should handle empty ip in response', async () => {
      mockedAxios.post.mockResolvedValueOnce({
        data: [{
          tags: { ip: undefined },
        }],
      });
      mockedAxios.post.mockResolvedValueOnce({
        data: [{
          tags: { ip: '10.0.0.1' },
        }],
      });

      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        maxQueryDays: 1,
        maxQueryRangeMs: 24 * 60 * 60 * 1000,
      });
      const hostIp = await resolver.resolve('sandbox-empty-ip');
      
      expect(hostIp).toBe('10.0.0.1');
    });
  });

  describe('time segmentation', () => {
    test('should segment queries when maxQueryDays exceeds maxQueryRangeMs', async () => {
      // Configure to require multiple segments
      // 3 days / 1 day per segment = 3 segments
      mockedAxios.post.mockResolvedValue({
        data: [], // No results, to force all segments to be queried
      });

      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        tenants: ['single_tenant'], // Use single tenant to simplify
        maxQueryDays: 3,
        maxQueryRangeMs: 24 * 60 * 60 * 1000, // 1 day
      });

      try {
        await resolver.resolve('sandbox-segments');
      } catch {
        // Expected to throw "not found"
      }

      // Should have called 3 times (3 segments)
      expect(mockedAxios.post).toHaveBeenCalledTimes(3);
    });

    test('should stop querying segments once hostIp is found', async () => {
      // First segment returns result immediately
      mockedAxios.post.mockResolvedValueOnce({
        data: [{
          tags: { ip: '192.168.1.1' },
        }],
      });

      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        tenants: ['single_tenant'],
        maxQueryDays: 7,
        maxQueryRangeMs: 24 * 60 * 60 * 1000,
      });

      const hostIp = await resolver.resolve('sandbox-early-find');
      
      expect(hostIp).toBe('192.168.1.1');
      // Should only have called once (found in first segment)
      expect(mockedAxios.post).toHaveBeenCalledTimes(1);
    });

    test('should query correct time ranges', async () => {
      const now = Date.now();
      const oneDayMs = 24 * 60 * 60 * 1000;

      mockedAxios.post.mockResolvedValueOnce({
        data: [{
          tags: { ip: '192.168.1.1' },
        }],
      });

      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        tenants: ['single_tenant'],
        maxQueryDays: 2,
        maxQueryRangeMs: 2 * oneDayMs, // 2 days = single segment
      });

      await resolver.resolve('sandbox-time-range');

      // Verify the time range in the API call
      expect(mockedAxios.post).toHaveBeenCalledTimes(1);
      const callArgs = mockedAxios.post.mock.calls[0];
      expect(callArgs).toBeDefined();
      const requestBody = callArgs![1] as { start: number; end: number };
      
      // start should be ~2 days ago, end should be ~now
      expect(requestBody.end).toBeGreaterThanOrEqual(now - 1000);
      expect(requestBody.start).toBeLessThan(requestBody.end);
      expect(requestBody.end - requestBody.start).toBeLessThanOrEqual(2 * oneDayMs);
    });
  });

  describe('rate limiting', () => {
    test('should wait between requests to respect QPS limit', async () => {
      // Need at least 2 requests to test rate limiting
      mockedAxios.post.mockResolvedValue({
        data: [],
      });

      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        tenants: ['single_tenant'],
        maxQueryDays: 2,
        maxQueryRangeMs: 24 * 60 * 60 * 1000, // 1 day, so 2 segments
      });

      const startTime = Date.now();
      
      try {
        await resolver.resolve('sandbox-rate-limit');
      } catch {
        // Expected to throw "not found"
      }

      const elapsed = Date.now() - startTime;
      
      // Should have 2 requests with at least 200ms between them
      expect(mockedAxios.post).toHaveBeenCalledTimes(2);
      // Total time should be at least 200ms (1 interval between 2 requests)
      expect(elapsed).toBeGreaterThanOrEqual(190); // Allow some tolerance
    });

    test('should not rate limit first request', async () => {
      mockedAxios.post.mockResolvedValueOnce({
        data: [{
          tags: { ip: '192.168.1.1' },
        }],
      });

      const resolver = new KmonHostIpResolver({
        token: 'test-token',
        tenants: ['single_tenant'],
        maxQueryDays: 1,
        maxQueryRangeMs: 24 * 60 * 60 * 1000,
      });

      const startTime = Date.now();
      await resolver.resolve('sandbox-first-request');
      const elapsed = Date.now() - startTime;

      // First request should complete quickly (no rate limit wait)
      expect(elapsed).toBeLessThan(100);
    });
  });
});
