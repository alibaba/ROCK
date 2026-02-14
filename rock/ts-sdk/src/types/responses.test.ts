/**
 * Tests for API Response parsing
 * 
 * Verifies that API responses are correctly parsed with camelCase fields
 * (HTTP layer converts snake_case from API to camelCase)
 */

import {
  SandboxStatusResponseSchema,
  IsAliveResponseSchema,
  CommandResponseSchema,
  ObservationSchema,
} from './responses.js';

describe('SandboxStatusResponse', () => {
  // Response after HTTP layer conversion (camelCase)
  const convertedResponse = {
    sandboxId: '295264ad162d43e6af25cf7974a76657',
    status: {
      imagePull: {
        status: 'success',
        message: 'use cached image, skip image pull',
      },
      dockerRun: {
        status: 'success',
        message: 'docker run success',
      },
    },
    state: null,
    portMapping: {
      '22555': 50787,
      '22': 26571,
      '8080': 48803,
    },
    hostName: 'etao-jqb011166008116.na131',
    hostIp: '11.166.8.116',
    isAlive: true,
    image: 'python:3.11',
    gatewayVersion: '0.0.45',
    sweRexVersion: '1.2.17',
    userId: 'default',
    experimentId: 'default',
    namespace: 'default',
    cpus: 2.0,
    memory: '8g',
  };

  test('should parse converted response correctly', () => {
    const result = SandboxStatusResponseSchema.parse(convertedResponse);

    expect(result.sandboxId).toBe('295264ad162d43e6af25cf7974a76657');
    expect(result.hostName).toBe('etao-jqb011166008116.na131');
    expect(result.hostIp).toBe('11.166.8.116');
    expect(result.isAlive).toBe(true);
    expect(result.image).toBe('python:3.11');
    expect(result.gatewayVersion).toBe('0.0.45');
    expect(result.sweRexVersion).toBe('1.2.17');
    expect(result.userId).toBe('default');
    expect(result.experimentId).toBe('default');
    expect(result.namespace).toBe('default');
    expect(result.cpus).toBe(2.0);
    expect(result.memory).toBe('8g');
  });

  test('should parse portMapping correctly', () => {
    const result = SandboxStatusResponseSchema.parse(convertedResponse);

    expect(result.portMapping).toEqual({
      '22555': 50787,
      '22': 26571,
      '8080': 48803,
    });
  });

  test('should parse status object correctly', () => {
    const result = SandboxStatusResponseSchema.parse(convertedResponse);

    expect(result.status).toEqual({
      imagePull: {
        status: 'success',
        message: 'use cached image, skip image pull',
      },
      dockerRun: {
        status: 'success',
        message: 'docker run success',
      },
    });
  });

  test('should handle minimal response', () => {
    const minimalResponse = {
      sandboxId: 'test-id',
      isAlive: true,
    };

    const result = SandboxStatusResponseSchema.parse(minimalResponse);

    expect(result.sandboxId).toBe('test-id');
    expect(result.isAlive).toBe(true);
    expect(result.hostName).toBeUndefined();
    expect(result.image).toBeUndefined();
  });

  test('should default isAlive to true if not provided', () => {
    const response = {
      sandboxId: 'test-id',
    };

    const result = SandboxStatusResponseSchema.parse(response);

    expect(result.isAlive).toBe(true);
  });
});

describe('IsAliveResponse', () => {
  test('should parse isAlive field correctly', () => {
    const result = IsAliveResponseSchema.parse({
      isAlive: true,
      message: 'host-name',
    });

    expect(result.isAlive).toBe(true);
    expect(result.message).toBe('host-name');
  });

  test('should default message to empty string', () => {
    const result = IsAliveResponseSchema.parse({
      isAlive: false,
    });

    expect(result.message).toBe('');
  });
});

describe('CommandResponse', () => {
  test('should parse with camelCase fields', () => {
    const result = CommandResponseSchema.parse({
      stdout: 'output',
      stderr: '',
      exitCode: 0,
    });

    expect(result.stdout).toBe('output');
    expect(result.stderr).toBe('');
    expect(result.exitCode).toBe(0);
  });

  test('should default stdout and stderr to empty strings', () => {
    const result = CommandResponseSchema.parse({});

    expect(result.stdout).toBe('');
    expect(result.stderr).toBe('');
  });
});

describe('Observation', () => {
  test('should parse with camelCase fields', () => {
    const result = ObservationSchema.parse({
      output: 'command output',
      exitCode: 0,
      failureReason: '',
      expectString: '',
    });

    expect(result.output).toBe('command output');
    expect(result.exitCode).toBe(0);
    expect(result.failureReason).toBe('');
    expect(result.expectString).toBe('');
  });

  test('should handle error response', () => {
    const result = ObservationSchema.parse({
      output: '',
      exitCode: 1,
      failureReason: 'Command failed',
      expectString: '',
    });

    expect(result.exitCode).toBe(1);
    expect(result.failureReason).toBe('Command failed');
  });

  test('should default optional fields', () => {
    const result = ObservationSchema.parse({
      output: 'test',
    });

    expect(result.exitCode).toBeUndefined();
    expect(result.failureReason).toBe('');
    expect(result.expectString).toBe('');
  });
});