/**
 * Tests for Sandbox Config
 */

import {
  SandboxConfigSchema,
  SandboxGroupConfigSchema,
  createSandboxConfig,
  createSandboxGroupConfig,
} from './config.js';
import { envVars } from '../env_vars.js';

describe('SandboxConfigSchema', () => {
  test('should use default values', () => {
    const config = SandboxConfigSchema.parse({});
    expect(config.image).toBe('python:3.11');
    expect(config.autoClearSeconds).toBe(300);
    expect(config.memory).toBe('8g');
    expect(config.cpus).toBe(2);
    expect(config.cluster).toBe('zb');
  });

  test('should allow custom values', () => {
    const config = SandboxConfigSchema.parse({
      image: 'node:18',
      memory: '16g',
      cpus: 4,
      cluster: 'custom',
    });
    expect(config.image).toBe('node:18');
    expect(config.memory).toBe('16g');
    expect(config.cpus).toBe(4);
    expect(config.cluster).toBe('custom');
  });

  test('should allow extra headers', () => {
    const config = SandboxConfigSchema.parse({
      extraHeaders: { 'X-Custom': 'value' },
    });
    expect(config.extraHeaders).toEqual({ 'X-Custom': 'value' });
  });
});

describe('SandboxGroupConfigSchema', () => {
  test('should use default values', () => {
    const config = SandboxGroupConfigSchema.parse({});
    expect(config.size).toBe(2);
    expect(config.startConcurrency).toBe(2);
    expect(config.startRetryTimes).toBe(3);
  });

  test('should extend SandboxConfig', () => {
    const config = SandboxGroupConfigSchema.parse({
      image: 'python:3.12',
      size: 5,
    });
    expect(config.image).toBe('python:3.12');
    expect(config.size).toBe(5);
  });
});

describe('createSandboxConfig', () => {
  test('should create config with defaults', () => {
    const config = createSandboxConfig();
    expect(config.image).toBe('python:3.11');
    expect(config.cluster).toBe('zb');
  });

  test('should merge partial config', () => {
    const config = createSandboxConfig({ image: 'custom:latest' });
    expect(config.image).toBe('custom:latest');
    expect(config.cluster).toBe('zb');
  });
});

describe('createSandboxGroupConfig', () => {
  test('should create group config with defaults', () => {
    const config = createSandboxGroupConfig();
    expect(config.size).toBe(2);
    expect(config.startConcurrency).toBe(2);
  });

  test('should merge partial config', () => {
    const config = createSandboxGroupConfig({ size: 10, startConcurrency: 5 });
    expect(config.size).toBe(10);
    expect(config.startConcurrency).toBe(5);
  });
});

describe('Config uses envVars (not hardcoded)', () => {
  // These tests verify that config schemas read defaults from envVars
  // rather than using hardcoded values.
  // This enables users to override defaults via environment variables.

  test('SandboxConfigSchema should use envVars for all default values', () => {
    const config = SandboxConfigSchema.parse({});
    // These assertions prove that config reads from envVars, not hardcoded strings
    expect(config.image).toBe(envVars.ROCK_DEFAULT_IMAGE);
    expect(config.memory).toBe(envVars.ROCK_DEFAULT_MEMORY);
    expect(config.cpus).toBe(envVars.ROCK_DEFAULT_CPUS);
    expect(config.cluster).toBe(envVars.ROCK_DEFAULT_CLUSTER);
    expect(config.autoClearSeconds).toBe(envVars.ROCK_DEFAULT_AUTO_CLEAR_SECONDS);
  });

  test('SandboxGroupConfigSchema should use envVars for group defaults', () => {
    const config = SandboxGroupConfigSchema.parse({});
    expect(config.size).toBe(envVars.ROCK_DEFAULT_GROUP_SIZE);
    expect(config.startConcurrency).toBe(envVars.ROCK_DEFAULT_START_CONCURRENCY);
    expect(config.startRetryTimes).toBe(envVars.ROCK_DEFAULT_START_RETRY_TIMES);
  });
});
