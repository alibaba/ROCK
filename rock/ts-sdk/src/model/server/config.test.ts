/**
 * Tests for model/server/config.ts
 */

import { ModelServiceConfigSchema, createModelServiceConfig } from './config.js';

describe('ModelServiceConfigSchema', () => {
  it('applies defaults for all fields', () => {
    const result = ModelServiceConfigSchema.parse({});

    expect(result.host).toBe('0.0.0.0');
    expect(result.port).toBe(8080);
    expect(result.proxy_base_url).toBeNull();
    expect(result.proxy_rules).toEqual({
      'gpt-3.5-turbo': 'https://api.openai.com/v1',
      default: 'https://api-inference.modelscope.cn/v1',
    });
    expect(result.retryable_status_codes).toEqual([429, 500]);
    expect(result.request_timeout).toBe(120);
    expect(result.recording_file).toBeNull();
    expect(result.replay_file).toBeNull();
  });

  it('accepts custom values for all fields', () => {
    const result = ModelServiceConfigSchema.parse({
      host: '127.0.0.1',
      port: 9999,
      proxy_base_url: 'https://custom.api.com/v1',
      proxy_rules: { 'my-model': 'https://my-backend.com/v1' },
      retryable_status_codes: [429, 500, 502],
      request_timeout: 300,
      recording_file: '/tmp/rec.jsonl',
      replay_file: undefined,
    });

    expect(result.host).toBe('127.0.0.1');
    expect(result.port).toBe(9999);
    expect(result.proxy_base_url).toBe('https://custom.api.com/v1');
    expect(result.proxy_rules).toEqual({ 'my-model': 'https://my-backend.com/v1' });
    expect(result.retryable_status_codes).toEqual([429, 500, 502]);
    expect(result.request_timeout).toBe(300);
    expect(result.recording_file).toBe('/tmp/rec.jsonl');
    expect(result.replay_file).toBeNull();
  });

  it('rejects recording_file and replay_file both set', () => {
    const result = ModelServiceConfigSchema.safeParse({
      recording_file: '/tmp/rec.jsonl',
      replay_file: '/tmp/replay.jsonl',
    });

    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues[0]?.message).toContain('mutually exclusive');
    }
  });

  it('allows only recording_file set', () => {
    const result = ModelServiceConfigSchema.safeParse({
      recording_file: '/tmp/rec.jsonl',
    });

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.recording_file).toBe('/tmp/rec.jsonl');
      expect(result.data.replay_file).toBeNull();
    }
  });

  it('allows only replay_file set', () => {
    const result = ModelServiceConfigSchema.safeParse({
      replay_file: '/tmp/replay.jsonl',
    });

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.replay_file).toBe('/tmp/replay.jsonl');
      expect(result.data.recording_file).toBeNull();
    }
  });
});

describe('createModelServiceConfig', () => {
  it('returns default config when called with no arguments', () => {
    const config = createModelServiceConfig();

    expect(config.host).toBe('0.0.0.0');
    expect(config.port).toBe(8080);
  });

  it('merges partial overrides with defaults', () => {
    const config = createModelServiceConfig({
      host: '10.0.0.1',
      port: 3000,
    });

    expect(config.host).toBe('10.0.0.1');
    expect(config.port).toBe(3000);
    // defaults still present
    expect(config.request_timeout).toBe(120);
    expect(config.retryable_status_codes).toEqual([429, 500]);
  });
});
