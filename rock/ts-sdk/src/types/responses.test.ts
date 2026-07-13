/**
 * Tests for API Response parsing
 * 
 * Verifies that API responses are correctly parsed with camelCase fields
 * (HTTP layer converts snake_case from API to camelCase)
 */

import {
  SandboxStatusResponseSchema,
  SandboxState,
  StateTransitionRecordSchema,
  IsAliveResponseSchema,
  CommandResponseSchema,
  ObservationSchema,
  DownloadFileResponseSchema,
  OssCredentialsSchema,
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

describe('DownloadFileResponse', () => {
  test('should parse successful response', () => {
    const result = DownloadFileResponseSchema.parse({
      success: true,
      message: 'Successfully downloaded /remote/file.txt to /local/file.txt',
    });

    expect(result.success).toBe(true);
    expect(result.message).toBe('Successfully downloaded /remote/file.txt to /local/file.txt');
  });

  test('should parse failure response', () => {
    const result = DownloadFileResponseSchema.parse({
      success: false,
      message: 'OSS download is not enabled',
    });

    expect(result.success).toBe(false);
    expect(result.message).toBe('OSS download is not enabled');
  });

  test('should default success to false', () => {
    const result = DownloadFileResponseSchema.parse({
      message: 'Error occurred',
    });

    expect(result.success).toBe(false);
  });

  test('should default message to empty string', () => {
    const result = DownloadFileResponseSchema.parse({});

    expect(result.message).toBe('');
  });
});

describe('OssCredentials', () => {
  test('should parse valid OSS credentials', () => {
    const result = OssCredentialsSchema.parse({
      accessKeyId: 'STS.NUxxxxxxxxxxxxxx',
      accessKeySecret: 'xxxxxxxxxxxxxxxx',
      securityToken: 'CAISxxxxxxxxxxxxxxxx',
      expiration: '2025-03-28T12:00:00Z',
    });

    expect(result.accessKeyId).toBe('STS.NUxxxxxxxxxxxxxx');
    expect(result.accessKeySecret).toBe('xxxxxxxxxxxxxxxx');
    expect(result.securityToken).toBe('CAISxxxxxxxxxxxxxxxx');
    expect(result.expiration).toBe('2025-03-28T12:00:00Z');
  });

  test('should require all fields', () => {
    expect(() => {
      OssCredentialsSchema.parse({
        accessKeyId: 'test',
        // missing other fields
      });
    }).toThrow();
  });

  test('should reject invalid types', () => {
    expect(() => {
      OssCredentialsSchema.parse({
        accessKeyId: 123, // should be string
        accessKeySecret: 'secret',
        securityToken: 'token',
        expiration: '2025-03-28T12:00:00Z',
      });
    }).toThrow();
  });
});

describe('SandboxState', () => {
  test('should define all state values', () => {
    expect(SandboxState.PENDING).toBe('pending');
    expect(SandboxState.RUNNING).toBe('running');
    expect(SandboxState.STOPPED).toBe('stopped');
    expect(SandboxState.ARCHIVING).toBe('archiving');
    expect(SandboxState.ARCHIVED).toBe('archived');
    expect(SandboxState.DELETED).toBe('deleted');
  });
});

describe('StateTransitionRecord', () => {
  test('should parse valid record', () => {
    const result = StateTransitionRecordSchema.parse({
      fromState: 'stopped',
      toState: 'archiving',
      event: 'archive',
      timestamp: '2026-07-09T10:00:00+08:00',
    });
    expect(result.fromState).toBe('stopped');
    expect(result.toState).toBe('archiving');
    expect(result.event).toBe('archive');
    expect(result.timestamp).toBe('2026-07-09T10:00:00+08:00');
  });
});

describe('SandboxStatusResponse - new fields', () => {
  test('should parse state as enum value', () => {
    const result = SandboxStatusResponseSchema.parse({
      sandboxId: 'test-id',
      isAlive: true,
      state: 'running',
    });
    expect(result.state).toBe('running');
  });

  test('should parse archiving and archived states', () => {
    const archiving = SandboxStatusResponseSchema.parse({ state: 'archiving' });
    expect(archiving.state).toBe('archiving');

    const archived = SandboxStatusResponseSchema.parse({ state: 'archived' });
    expect(archived.state).toBe('archived');
  });

  test('should default state to null when not provided', () => {
    const result = SandboxStatusResponseSchema.parse({ sandboxId: 'test-id' });
    expect(result.state).toBeNull();
  });

  test('should accept null state', () => {
    const result = SandboxStatusResponseSchema.parse({ state: null });
    expect(result.state).toBeNull();
  });

  test('should parse disk field', () => {
    const result = SandboxStatusResponseSchema.parse({
      sandboxId: 'test-id',
      disk: '20g',
    });
    expect(result.disk).toBe('20g');
  });

  test('should default disk to null', () => {
    const result = SandboxStatusResponseSchema.parse({ sandboxId: 'test-id' });
    expect(result.disk).toBeNull();
  });

  test('should parse stateHistory', () => {
    const result = SandboxStatusResponseSchema.parse({
      sandboxId: 'test-id',
      stateHistory: [
        { fromState: 'pending', toState: 'running', event: 'alive', timestamp: '2026-07-09T10:00:00+08:00' },
        { fromState: 'running', toState: 'stopped', event: 'stop', timestamp: '2026-07-09T11:00:00+08:00' },
      ],
    });
    expect(result.stateHistory).toHaveLength(2);
    expect(result.stateHistory[0]!.fromState).toBe('pending');
    expect(result.stateHistory[1]!.event).toBe('stop');
  });

  test('should default stateHistory to empty array', () => {
    const result = SandboxStatusResponseSchema.parse({ sandboxId: 'test-id' });
    expect(result.stateHistory).toEqual([]);
  });

  test('backward compatibility: existing response without new fields parses correctly', () => {
    const legacyResponse = {
      sandboxId: '295264ad162d43e6af25cf7974a76657',
      hostName: 'test-host',
      hostIp: '11.166.8.116',
      isAlive: true,
      image: 'python:3.11',
      cpus: 2.0,
      memory: '8g',
    };
    const result = SandboxStatusResponseSchema.parse(legacyResponse);
    expect(result.sandboxId).toBe('295264ad162d43e6af25cf7974a76657');
    expect(result.state).toBeNull();
    expect(result.disk).toBeNull();
    expect(result.stateHistory).toEqual([]);
  });
});