/**
 * Tests for job/trial/abstract.ts — AbstractTrial base class
 */

import { z } from 'zod';
import { SandboxConfigSchema } from '../../sandbox/config';

// Minimal environment schema for tests
const EnvironmentConfigSchema = SandboxConfigSchema.extend({
  uploads: z.array(z.tuple([z.string(), z.string()])).default([]),
  env: z.record(z.string()).default({}),
  oss_mirror: z.any().nullable().default(null),
  proxy: z.any().nullable().default(null),
  tracking: z.any().nullable().default(null),
});

import { BashJobConfigSchema } from '../config';
import { TrialResult } from '../result';

// We'll test AbstractTrial indirectly via a concrete subclass
import { AbstractTrial } from './abstract';

// ---------------------------------------------------------------------------
// Test Trial — minimal concrete subclass for testing
// ---------------------------------------------------------------------------

interface SandboxLike {
  getNamespace(): string | null;
  getExperimentId(): string | null;
}

class FakeSandbox implements SandboxLike {
  namespace: string | null = null;
  experimentId: string | null = null;

  getNamespace(): string | null { return this.namespace; }
  getExperimentId(): string | null { return this.experimentId; }
}

class TestTrial extends AbstractTrial {
  build(): string {
    return '#!/bin/bash\necho "hello from test"';
  }

  async collect(): Promise<TrialResult> {
    return { task_name: 'test', exception_info: null, started_at: null, finished_at: null, raw_output: '', exit_code: 0, score: 0, status: 'completed', duration_sec: 0 };
  }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AbstractTrial', () => {
  const schema = BashJobConfigSchema(EnvironmentConfigSchema);

  describe('constructor', () => {
    test('stores config reference', () => {
      const config = schema.parse({ script: 'echo hi' });
      const trial = new TestTrial(config as any);
      expect(trial.config).toBe(config);
    });

    test('config is publicly readable', () => {
      const config = schema.parse({ script: 'echo test', job_name: 'my-test' });
      const trial = new TestTrial(config as any);
      expect(trial.config.job_name).toBe('my-test');
    });
  });

  describe('onSandboxReady', () => {
    test('backfills namespace from sandbox when not set', async () => {
      const config = schema.parse({ script: 'echo hi' });
      const trial = new TestTrial(config as any);
      const sandbox = new FakeSandbox();
      sandbox.namespace = 'test-ns';

      await trial.onSandboxReady(sandbox as any);
      expect((trial.config as any).namespace).toBe('test-ns');
    });

    test('does not overwrite existing namespace', async () => {
      const config = schema.parse({ script: 'echo hi', namespace: 'existing-ns' });
      const trial = new TestTrial(config as any);
      const sandbox = new FakeSandbox();
      sandbox.namespace = 'new-ns';

      await trial.onSandboxReady(sandbox as any);
      expect((trial.config as any).namespace).toBe('existing-ns');
    });

    test('backfills experiment_id when config has none', async () => {
      const config = schema.parse({ script: 'echo hi' });
      const trial = new TestTrial(config as any);
      const sandbox = new FakeSandbox();
      sandbox.experimentId = 'exp-001';

      await trial.onSandboxReady(sandbox as any);
      expect((trial.config as any).experiment_id).toBe('exp-001');
    });

    test('config experiment_id takes priority over sandbox value', async () => {
      const config = schema.parse({ script: 'echo hi', experiment_id: 'config-exp' });
      const trial = new TestTrial(config as any);
      const sandbox = new FakeSandbox();
      sandbox.experimentId = 'sandbox-exp';

      await trial.onSandboxReady(sandbox as any);
      expect((trial.config as any).experiment_id).toBe('config-exp');
    });

    test('handles null values from sandbox gracefully', async () => {
      const config = schema.parse({ script: 'echo hi' });
      const trial = new TestTrial(config as any);
      const sandbox = new FakeSandbox();
      // Both null — should not throw

      await trial.onSandboxReady(sandbox as any);
      expect((trial.config as any).namespace).toBeNull();
      expect((trial.config as any).experiment_id).toBeNull();
    });
  });

  describe('build', () => {
    test('is abstract — subclass must implement', () => {
      const config = schema.parse({ script: 'echo hi' });
      const trial = new TestTrial(config as any);
      expect(trial.build()).toBe('#!/bin/bash\necho "hello from test"');
    });
  });

  describe('collect', () => {
    test('is abstract — subclass must implement', async () => {
      const config = schema.parse({ script: 'echo hi' });
      const trial = new TestTrial(config as any);
      const result = await trial.collect();
      expect(result.task_name).toBe('test');
      expect(result.status).toBe('completed');
    });
  });
});
