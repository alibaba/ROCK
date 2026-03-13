/**
 * Tests for EnvHub Schema
 */

import {
  EnvHubClientConfigSchema,
  RockEnvInfoSchema,
  createRockEnvInfo,
} from './schema.js';

describe('EnvHubClientConfigSchema', () => {
  test('should use default baseUrl', () => {
    const config = EnvHubClientConfigSchema.parse({});
    expect(config.baseUrl).toBe('http://localhost:8081');
  });

  test('should allow custom baseUrl', () => {
    const config = EnvHubClientConfigSchema.parse({
      baseUrl: 'http://custom:9000',
    });
    expect(config.baseUrl).toBe('http://custom:9000');
  });
});

describe('RockEnvInfoSchema', () => {
  test('should parse valid data', () => {
    const env = RockEnvInfoSchema.parse({
      envName: 'test-env',
      image: 'python:3.11',
    });

    expect(env.envName).toBe('test-env');
    expect(env.image).toBe('python:3.11');
    expect(env.owner).toBe('');
    expect(env.tags).toEqual([]);
  });

  test('should use defaults for optional fields', () => {
    const env = RockEnvInfoSchema.parse({
      envName: 'test',
      image: 'node:18',
    });

    expect(env.description).toBe('');
    expect(env.createAt).toBe('');
    expect(env.updateAt).toBe('');
  });
});

describe('createRockEnvInfo', () => {
  test('should parse camelCase data (after HTTP layer conversion)', () => {
    const env = createRockEnvInfo({
      envName: 'test-env',
      image: 'python:3.11',
      createAt: '2024-01-01',
      extraSpec: { key: 'value' },
    });

    expect(env.envName).toBe('test-env');
    expect(env.createAt).toBe('2024-01-01');
    expect(env.extraSpec).toEqual({ key: 'value' });
  });

  test('should handle minimal data', () => {
    const env = createRockEnvInfo({
      envName: 'test-env',
      image: 'python:3.11',
    });

    expect(env.envName).toBe('test-env');
    expect(env.image).toBe('python:3.11');
  });
});