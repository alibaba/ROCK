/**
 * Tests for job/config.ts — JobConfig (base), BashJobConfig
 */

import { z } from 'zod';

// Import from Sandbox config for the environment field (used by JobConfig)
import { SandboxConfigSchema } from '../sandbox/config';

// Placeholder EnvironmentConfig — until envhub schema provides this, we define
// a minimal version inline matching Python EnvironmentConfig(SandboxConfig).
const EnvironmentConfigSchema = SandboxConfigSchema.extend({
  uploads: z.array(z.tuple([z.string(), z.string()])).default([]),
  env: z.record(z.string()).default({}),
  oss_mirror: z.any().nullable().default(null),
  proxy: z.any().nullable().default(null),
  tracking: z.any().nullable().default(null),
});

import {
  JobConfigSchema,
  JobConfig,
  BashJobConfigSchema,
  BashJobConfig,
  createJobConfig,
  createBashJobConfig,
} from './config';

// ---------------------------------------------------------------------------
// JobConfig (base)
// ---------------------------------------------------------------------------
describe('JobConfig', () => {
  describe('JobConfigSchema', () => {
    test('parses empty object with defaults', () => {
      const schema = JobConfigSchema(EnvironmentConfigSchema);
      const result = schema.parse({});
      // base fields
      expect(result.job_name).toBeNull();
      expect(result.namespace).toBeNull();
      expect(result.experiment_id).toBeNull();
      expect(result.labels).toEqual({});
      expect(result.timeout).toBe(7200);
      // environment field should have defaults
      expect(result.environment).toBeDefined();
      expect(result.environment.uploads).toEqual([]);
      expect(result.environment.env).toEqual({});
    });

    test('parses with custom fields', () => {
      const schema = JobConfigSchema(EnvironmentConfigSchema);
      const result = schema.parse({
        job_name: 'my-job',
        namespace: 'test-ns',
        experiment_id: 'exp-123',
        labels: { env: 'test' },
        timeout: 3600,
      });
      expect(result.job_name).toBe('my-job');
      expect(result.namespace).toBe('test-ns');
      expect(result.experiment_id).toBe('exp-123');
      expect(result.labels).toEqual({ env: 'test' });
      expect(result.timeout).toBe(3600);
    });

    test('syncs experiment_id to environment', () => {
      const schema = JobConfigSchema(EnvironmentConfigSchema);
      const result = schema.parse({
        experiment_id: 'exp-456',
      });
      // experiment_id should be propagated to environment
      expect(result.environment.experimentId).toBe('exp-456');
    });

    test('does not sync experiment_id when not set', () => {
      const schema = JobConfigSchema(EnvironmentConfigSchema);
      const result = schema.parse({});
      expect(result.experiment_id).toBeNull();
      // environment.experimentId is nullable and defaults to undefined (optional in SandboxConfig)
    });

    test('warns when experiment_id conflicts between job and environment', () => {
      const schema = JobConfigSchema(EnvironmentConfigSchema);
      // When both are set and differ, job.experiment_id wins
      const result = schema.parse({
        experiment_id: 'job-exp',
        environment: {
          experimentId: 'env-exp',
        },
      });
      // job.experiment_id should take priority
      expect(result.experiment_id).toBe('job-exp');
      // environment should be synced to job value
      expect(result.environment.experimentId).toBe('job-exp');
    });
  });

  describe('createJobConfig', () => {
    test('creates JobConfig with defaults', () => {
      const config = createJobConfig({}, EnvironmentConfigSchema);
      expect(config.job_name).toBeNull();
      expect(config.timeout).toBe(7200);
    });

    test('creates JobConfig with custom values', () => {
      const config = createJobConfig({
        job_name: 'test',
        timeout: 1800,
      }, EnvironmentConfigSchema);
      expect(config.job_name).toBe('test');
      expect(config.timeout).toBe(1800);
    });
  });
});

// ---------------------------------------------------------------------------
// BashJobConfig
// ---------------------------------------------------------------------------
describe('BashJobConfig', () => {
  describe('BashJobConfigSchema', () => {
    test('parses empty object with defaults', () => {
      const schema = BashJobConfigSchema(EnvironmentConfigSchema);
      const result = schema.parse({});
      // job_name should be auto-generated timestamp
      expect(result.job_name).toBeDefined();
      expect(typeof result.job_name).toBe('string');
      expect(result.job_name.length).toBeGreaterThan(0);
      expect(result.script).toBeNull();
      expect(result.script_path).toBeNull();
    });

    test('parses with custom script', () => {
      const schema = BashJobConfigSchema(EnvironmentConfigSchema);
      const result = schema.parse({
        job_name: 'bash-test',
        script: 'echo hello',
        experiment_id: 'exp-1',
        timeout: 3600,
      });
      expect(result.job_name).toBe('bash-test');
      expect(result.script).toBe('echo hello');
      expect(result.experiment_id).toBe('exp-1');
      expect(result.timeout).toBe(3600);
    });

    test('rejects extra unknown fields', () => {
      const schema = BashJobConfigSchema(EnvironmentConfigSchema);
      expect(() =>
        schema.parse({
          script: 'echo hi',
          unknown_field: 'should fail',
        })
      ).toThrow();
    });

    test('extends from JobConfig and inherits all base fields', () => {
      const schema = BashJobConfigSchema(EnvironmentConfigSchema);
      const result = schema.parse({
        namespace: 'ns-1',
        labels: { key: 'val' },
        script: 'echo inherits',
      });
      expect(result.namespace).toBe('ns-1');
      expect(result.labels).toEqual({ key: 'val' });
      expect(result.script).toBe('echo inherits');
      expect(result.timeout).toBe(7200);
    });
  });

  describe('createBashJobConfig', () => {
    test('creates BashJobConfig with defaults', () => {
      const config = createBashJobConfig({}, EnvironmentConfigSchema);
      expect(config.script).toBeNull();
      expect(config.job_name).toBeDefined();
    });

    test('creates BashJobConfig with custom script', () => {
      const config = createBashJobConfig({
        job_name: 'my-bash',
        script: '#!/bin/bash\necho done',
      }, EnvironmentConfigSchema);
      expect(config.job_name).toBe('my-bash');
      expect(config.script).toBe('#!/bin/bash\necho done');
    });
  });
});
