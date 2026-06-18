/**
 * Tests for model/server/api/proxy.ts
 */

import { filterHeaders } from './proxy.js';

describe('filterHeaders', () => {
  it('drops host, content-length, transfer-encoding, connection', () => {
    const headers: Record<string, string> = {
      host: 'example.com',
      'content-length': '123',
      'transfer-encoding': 'chunked',
      connection: 'keep-alive',
      authorization: 'Bearer token123',
      'content-type': 'application/json',
    };

    const result = filterHeaders(headers);

    expect(result).not.toHaveProperty('host');
    expect(result).not.toHaveProperty('content-length');
    expect(result).not.toHaveProperty('transfer-encoding');
    expect(result).not.toHaveProperty('connection');
    expect(result.authorization).toBe('Bearer token123');
    expect(result['content-type']).toBe('application/json');
  });

  it('returns empty object for empty headers', () => {
    expect(filterHeaders({})).toEqual({});
  });

  it('handles case-insensitive header names', () => {
    const headers: Record<string, string> = {
      Host: 'example.com',
      'Content-Length': '100',
      Authorization: 'token',
    };

    const result = filterHeaders(headers);

    expect(result).not.toHaveProperty('Host');
    expect(result).not.toHaveProperty('Content-Length');
    expect(result.Authorization).toBe('token');
  });
});
