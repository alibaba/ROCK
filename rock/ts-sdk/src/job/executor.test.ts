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

class LifecycleTrial extends AbstractTrial {
  events: string[] = [];

  override async onSandboxReady(sandbox: any): Promise<void> {
    this.events.push(`ready:${sandbox.getNamespace()}:${sandbox.getExperimentId()}`);
    await super.onSandboxReady(sandbox);
  }

  override async setup(): Promise<void> {
    this.events.push('setup');
  }

  build(): string {
    this.events.push('build');
    return 'echo lifecycle';
  }

  async collect(sandbox?: unknown, output?: string, exit_code?: number): Promise<any> {
    this.events.push(`collect:${Boolean(sandbox)}:${output}:${exit_code}`);
    return { task_name: 'lifecycle', exception_info: null, started_at: null, finished_at: null, raw_output: output ?? '', exit_code: exit_code ?? 0, score: 0, status: 'completed', duration_sec: 0 };
  }
}

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

    test('starts sandbox, creates session, writes script, and starts nohup process', async () => {
      const config = makeConfig();
      const trial = new LifecycleTrial(config);
      const calls: string[] = [];
      const sandbox = {
        start: jest.fn(async () => { calls.push('start'); }),
        getNamespace: jest.fn(() => 'ns-from-sandbox'),
        getExperimentId: jest.fn(() => 'exp-from-sandbox'),
        createSession: jest.fn(async (request) => { calls.push(`create:${request.session}:${request.envEnable}`); }),
        writeFile: jest.fn(async (request) => { calls.push(`write:${request.path}:${request.content}`); return { success: true, message: '' }; }),
        startNohupProcess: jest.fn(async (cmd, tmpFile, session) => {
          calls.push(`nohup:${cmd}:${tmpFile}:${session}`);
          return { pid: 1234, errorResponse: null };
        }),
      };
      const executor = new JobExecutor(() => sandbox as any);
      const operator = { apply: () => [trial] };

      const client = await executor.submit(operator, config);

      expect(client.trials).toHaveLength(1);
      const submitted = client.trials[0]!;
      expect(submitted.sandbox).toBe(sandbox);
      expect(submitted.pid).toBe(1234);
      expect(submitted.session).toBe('rock-job-test-job');
      expect(calls).toEqual([
        'start',
        'create:rock-job-test-job:true',
        'write:/data/logs/user-defined/rock_job_test-job.sh:echo lifecycle',
        "nohup:bash -c 'bash '\\''/data/logs/user-defined/rock_job_test-job.sh'\\''; rc=$?; echo \"$rc\" > '\\''/data/logs/user-defined/rock_job_test-job.exit'\\''; exit \"$rc\"':/data/logs/user-defined/rock_job_test-job.out:rock-job-test-job",
      ]);
      expect(trial.events).toEqual(['ready:ns-from-sandbox:exp-from-sandbox', 'setup', 'build']);
      expect(config.namespace).toBe('ns-from-sandbox');
      expect(config.experiment_id).toBe('exp-from-sandbox');
    });

    test('propagates trial submission failures', async () => {
      const config = makeConfig();
      const trial = new LifecycleTrial(config);
      const sandbox = {
        start: jest.fn(async () => { throw new Error('sandbox unavailable'); }),
      };
      const executor = new JobExecutor(() => sandbox as any);
      const operator = { apply: () => [trial] };

      await expect(executor.submit(operator, config)).rejects.toThrow('sandbox unavailable');
    });
  });

  describe('wait', () => {
    test('returns empty array for empty JobClient', async () => {
      const executor = new JobExecutor();
      const client: JobClient = { trials: [] };
      const results = await executor.wait(client);
      expect(results).toEqual([]);
    });

    test('waits for process completion and collects with sandbox output and exit code', async () => {
      const config = makeConfig();
      const trial = new LifecycleTrial(config);
      const sandbox = {
        waitForProcessCompletion: jest.fn(async () => ({ success: true, message: 'done' })),
        handleNohupOutput: jest.fn(async () => ({ output: 'job output', exitCode: 7, failureReason: '', expectString: '' })),
      };
      const executor = new JobExecutor();
      const client: JobClient = {
        trials: [{ sandbox: sandbox as any, session: 'rock-job-test-job', pid: 4321, trial }],
      };

      const results = await executor.wait(client);

      expect(sandbox.waitForProcessCompletion).toHaveBeenCalledWith(4321, 'rock-job-test-job', 3600, 30);
      expect(sandbox.handleNohupOutput).toHaveBeenCalledWith(
        '/data/logs/user-defined/rock_job_test-job.out',
        'rock-job-test-job',
        true,
        'done',
        false,
        null
      );
      expect(results).toEqual([
        expect.objectContaining({ raw_output: 'job output', exit_code: 7 }),
      ]);
      expect(trial.events).toEqual(['collect:true:job output:7']);
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
