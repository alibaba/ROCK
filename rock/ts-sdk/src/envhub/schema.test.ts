/**
 * Tests for EnvHub Schema
 */

import {
  EnvHubClientConfigSchema,
  RockEnvInfoSchema,
  createRockEnvInfo,
  rockEnvInfoToDict,
} from './schema.js';
import type { RockEnvInfo } from './schema.js';

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
  test('should convert snake_case to camelCase', () => {
    const env = createRockEnvInfo({
      env_name: 'test-env',
      image: 'python:3.11',
      create_at: '2024-01-01',
      extra_spec: { key: 'value' },
    });

    expect(env.envName).toBe('test-env');
    expect(env.createAt).toBe('2024-01-01');
    expect(env.extraSpec).toEqual({ key: 'value' });
  });

  test('should accept camelCase input', () => {
    const env = createRockEnvInfo({
      envName: 'test-env',
      image: 'python:3.11',
    });

    expect(env.envName).toBe('test-env');
  });
});

describe('rockEnvInfoToDict', () => {
  test('should convert to snake_case', () => {
    const env: RockEnvInfo = {
      envName: 'test-env',
      image: 'python:3.11',
      owner: 'user',
      createAt: '2024-01-01',
      updateAt: '2024-01-02',
      description: 'test',
      tags: ['tag1'],
      extraSpec: { key: 'value' },
    };

    const dict = rockEnvInfoToDict(env);

    expect(dict.env_name).toBe('test-env');
    expect(dict.create_at).toBe('2024-01-01');
    expect(dict.extra_spec).toEqual({ key: 'value' });
  });
});
