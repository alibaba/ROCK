/**
 * Tests for API Response parsing
 * 
 * Verifies that API responses with snake_case fields are correctly parsed
 */

import {
  SandboxStatusResponseSchema,
  IsAliveResponseSchema,
  CommandResponseSchema,
  ObservationSchema,
} from './responses.js';

describe('SandboxStatusResponse', () => {
  // Real API response from get_status endpoint
  const realApiResponse = {
    sandbox_id: '295264ad162d43e6af25cf7974a76657',
    status: {
      image_pull: {
        status: 'success',
        message: 'use cached image, skip image pull',
      },
      docker_run: {
        status: 'success',
        message: 'docker run success',
      },
    },
    state: null,
    port_mapping: {
      '22555': 50787,
      '22': 26571,
      '8080': 48803,
    },
    host_name: 'etao-jqb011166008116.na131',
    host_ip: '11.166.8.116',
    is_alive: true,
    image: 'python:3.11',
    gateway_version: '0.0.45',
    swe_rex_version: '1.2.17',
    user_id: 'default',
    experiment_id: 'default',
    namespace: 'default',
    cpus: 2.0,
    memory: '8g',
  };

  test('should parse real API response correctly', () => {
    const result = SandboxStatusResponseSchema.parse(realApiResponse);

    expect(result.sandbox_id).toBe('295264ad162d43e6af25cf7974a76657');
    expect(result.host_name).toBe('etao-jqb011166008116.na131');
    expect(result.host_ip).toBe('11.166.8.116');
    expect(result.is_alive).toBe(true);
    expect(result.image).toBe('python:3.11');
    expect(result.gateway_version).toBe('0.0.45');
    expect(result.swe_rex_version).toBe('1.2.17');
    expect(result.user_id).toBe('default');
    expect(result.experiment_id).toBe('default');
    expect(result.namespace).toBe('default');
    expect(result.cpus).toBe(2.0);
    expect(result.memory).toBe('8g');
  });

  test('should parse port_mapping correctly', () => {
    const result = SandboxStatusResponseSchema.parse(realApiResponse);

    expect(result.port_mapping).toEqual({
      '22555': 50787,
      '22': 26571,
      '8080': 48803,
    });
  });

  test('should parse status object correctly', () => {
    const result = SandboxStatusResponseSchema.parse(realApiResponse);

    expect(result.status).toEqual({
      image_pull: {
        status: 'success',
        message: 'use cached image, skip image pull',
      },
      docker_run: {
        status: 'success',
        message: 'docker run success',
      },
    });
  });

  test('should handle minimal response', () => {
    const minimalResponse = {
      sandbox_id: 'test-id',
      is_alive: true,
    };

    const result = SandboxStatusResponseSchema.parse(minimalResponse);

    expect(result.sandbox_id).toBe('test-id');
    expect(result.is_alive).toBe(true);
    expect(result.host_name).toBeUndefined();
    expect(result.image).toBeUndefined();
  });

  test('should default is_alive to true if not provided', () => {
    const response = {
      sandbox_id: 'test-id',
    };

    const result = SandboxStatusResponseSchema.parse(response);

    expect(result.is_alive).toBe(true);
  });
});

describe('IsAliveResponse', () => {
  test('should parse is_alive field correctly', () => {
    const result = IsAliveResponseSchema.parse({
      is_alive: true,
      message: 'host-name',
    });

    expect(result.is_alive).toBe(true);
    expect(result.message).toBe('host-name');
  });

  test('should default message to empty string', () => {
    const result = IsAliveResponseSchema.parse({
      is_alive: false,
    });

    expect(result.message).toBe('');
  });
});

describe('CommandResponse', () => {
  test('should parse with snake_case fields', () => {
    const result = CommandResponseSchema.parse({
      stdout: 'output',
      stderr: '',
      exit_code: 0,
    });

    expect(result.stdout).toBe('output');
    expect(result.stderr).toBe('');
    expect(result.exit_code).toBe(0);
  });

  test('should default stdout and stderr to empty strings', () => {
    const result = CommandResponseSchema.parse({});

    expect(result.stdout).toBe('');
    expect(result.stderr).toBe('');
  });
});

describe('Observation', () => {
  test('should parse with snake_case fields', () => {
    const result = ObservationSchema.parse({
      output: 'command output',
      exit_code: 0,
      failure_reason: '',
      expect_string: '',
    });

    expect(result.output).toBe('command output');
    expect(result.exit_code).toBe(0);
    expect(result.failure_reason).toBe('');
    expect(result.expect_string).toBe('');
  });

  test('should handle error response', () => {
    const result = ObservationSchema.parse({
      output: '',
      exit_code: 1,
      failure_reason: 'Command failed',
      expect_string: '',
    });

    expect(result.exit_code).toBe(1);
    expect(result.failure_reason).toBe('Command failed');
  });

  test('should default optional fields', () => {
    const result = ObservationSchema.parse({
      output: 'test',
    });

    expect(result.exit_code).toBeUndefined();
    expect(result.failure_reason).toBe('');
    expect(result.expect_string).toBe('');
  });
});
