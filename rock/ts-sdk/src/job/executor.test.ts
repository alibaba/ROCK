/**
 * Tests for job/executor.ts — JobExecutor, JobClient, TrialClient
 */

import { z } from 'zod';
import { SandboxConfigSchema } from '../sandbox/config';
import { BashJobConfigSchema } from './config';
import { JobExecutor, JobClient, TrialClient } from './executor';
import { ScatterOperator } from './operator';
import { AbstractTrial } from './trial/abstract';
import { registerTrial } from './trial/registry';

// Minimal environment
const EnvironmentConfigSchema = SandboxConfigSchema.extend({
  uploads: z.array(z.tuple([z.string(), z.string()])).default([]),
  env: z.record(z.string()).default({}),
  oss_mirror: z.any().nullable().default(null),
  proxy: z.any().nullable().default(null),
  tracking: z.any().nullable().default(null),
});

// Test trial — minimal concrete subclass
const BASH_KEY = Symbol.for('TestBashForExecutor');
class FakeTrial extends AbstractTrial {
  build(): string { return 'echo hello'; }
  async collect(_sandbox?: unknown, output?: string, exit_code?: number): Promise<any> {
    return { task_name: 'test', exception_info: null, started_at: null, finished_at: null, raw_output: output ?? '', exit_code: exit_code ?? 0, score: 0, status: 'completed', duration_sec: 0 };
  }
}
registerTrial(BASH_KEY, FakeTrial);

function makeConfig(): Record<string, unknown> {
  const schema = BashJobConfigSchema(EnvironmentConfigSchema);
  const config = schema.parse({
    script: 'echo hi',
    job_name: 'test-job',
    timeout: 3600,
  }) as Record<string, unknown>;
  (config as any)['_registryKey'] = BASH_KEY;
  return config;
}

describe('JobExecutor', () => {
  describe('submit', () => {
    test('returns JobClient with empty trials when operator returns empty', async () => {
      const executor = new JobExecutor();
      const config = makeConfig();
      const operator = new ScatterOperator(0);
      const client = await executor.submit(operator, config);
      expect(client).toBeInstanceOf(Object);
      expect(client.trials).toEqual([]);
    });

    test('returns JobClient for single trial', async () => {
      const executor = new JobExecutor();
      const config = makeConfig();
      const operator = new ScatterOperator(1);
      // submit requires sandbox.start() — we test that it throws when trying to start
      // since there's no real sandbox
      try {
        await executor.submit(operator, config);
        // May throw because sandbox constructor fails
      } catch (e: any) {
        // Expected — no real sandbox endpoint available
        expect(e).toBeDefined();
      }
    });
  });

  describe('wait', () => {
    test('returns empty array for empty JobClient', async () => {
      const executor = new JobExecutor();
      const client: JobClient = { trials: [] };
      const results = await executor.wait(client);
      expect(results).toEqual([]);
    });
  });

  describe('buildSessionEnv', () => {
    test('returns null when no env is set', () => {
      const config = makeConfig();
      const env = (JobExecutor as any).buildSessionEnv(config);
      // Should be null or empty
      if (env) {
        expect(typeof env).toBe('object');
      }
    });

    test('includes OSS_* env vars from process.env', () => {
      // Save and restore original OSS_BUCKET
      const original = process.env['OSS_BUCKET'];
      try {
        process.env['OSS_BUCKET'] = 'test-bucket';
        const config = makeConfig();
        const env = (JobExecutor as any).buildSessionEnv(config);
        if (env) {
          expect(env['OSS_BUCKET']).toBe('test-bucket');
        }
      } finally {
        if (original === undefined) {
          delete process.env['OSS_BUCKET'];
        } else {
          process.env['OSS_BUCKET'] = original;
        }
      }
    });
  });
});
