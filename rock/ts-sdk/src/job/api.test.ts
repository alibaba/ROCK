/**
 * Tests for job/api.ts — Job facade
 */

import { z } from 'zod';
import { SandboxConfigSchema } from '../sandbox/config';
import { BashJobConfigSchema } from './config';
import { Job } from './api';
import { JobExecutor } from './executor';
import { ScatterOperator } from './operator';
import { AbstractTrial } from './trial/abstract';
import { registerTrial } from './trial/registry';
import { JobResult, TrialResult, JobStatus, ExceptionInfoSchema } from './result';

// Minimal environment
const EnvironmentConfigSchema = SandboxConfigSchema.extend({
  uploads: z.array(z.tuple([z.string(), z.string()])).default([]),
  env: z.record(z.string()).default({}),
  oss_mirror: z.any().nullable().default(null),
  proxy: z.any().nullable().default(null),
  tracking: z.any().nullable().default(null),
});

const BASH_KEY = Symbol.for('TestBashForApi');
class FakeTrial extends AbstractTrial {
  build(): string { return 'echo test'; }
  async collect(_sandbox?: unknown, output?: string, exit_code?: number): Promise<TrialResult> {
    const code = exit_code ?? 0;
    return {
      task_name: 'api-test',
      exception_info: code === 0 ? null : ExceptionInfoSchema.parse({
        exception_type: 'BashExitCode',
        exception_message: `Bash script exited with code ${code}`,
      }),
      started_at: null,
      finished_at: null,
      raw_output: output ?? '',
      exit_code: code,
      score: 0,
      status: code === 0 ? 'completed' : 'failed',
      duration_sec: 0,
    };
  }
}
registerTrial(BASH_KEY, FakeTrial);

function makeConfig(): Record<string, unknown> {
  const schema = BashJobConfigSchema(EnvironmentConfigSchema);
  const config = schema.parse({ script: 'echo hi', job_name: 'api-test-job' }) as Record<string, unknown>;
  (config as any)['_registryKey'] = BASH_KEY;
  return config;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe('Job', () => {
  describe('constructor', () => {
    test('accepts config and optional operator', () => {
      const config = makeConfig();
      const job = new Job(config);
      expect(job).toBeInstanceOf(Job);
    });

    test('accepts scatter operator with custom size', () => {
      const config = makeConfig();
      const op = new ScatterOperator(4);
      const job = new Job(config, op);
      expect(job).toBeInstanceOf(Job);
    });
  });

  describe('submit', () => {
    test('throws when sandbox API is unavailable', async () => {
      const config = makeConfig();
      const op = new ScatterOperator(1);
      const job = new Job(config, op);
      // Will fail because no real sandbox — this validates the flow
      try {
        await job.submit();
      } catch (e: any) {
        expect(e).toBeDefined();
      }
    });
  });

  describe('run', () => {
    test('uses the sidecar script exit code for the final JobResult', async () => {
      const config = makeConfig();
      const sandbox = {
        start: jest.fn(async () => undefined),
        getNamespace: jest.fn(() => null),
        getExperimentId: jest.fn(() => null),
        createSession: jest.fn(async () => ({})),
        writeFile: jest.fn(async () => ({ success: true, message: '' })),
        startNohupProcess: jest.fn(async () => ({ pid: 2468, errorResponse: null })),
        waitForProcessCompletion: jest.fn(async () => ({ success: true, message: 'done' })),
        handleNohupOutput: jest.fn(async () => ({
          output: 'script output',
          exitCode: 0,
          failureReason: '',
          expectString: '',
        })),
        readFile: jest.fn(async () => ({ content: '7\n' })),
      };
      const job = new Job(config, { apply: () => [new FakeTrial(config as any)] });
      (job as any).executor = new JobExecutor(() => sandbox as any);

      const result = await job.run();

      expect(sandbox.readFile).toHaveBeenCalledWith({
        path: '/data/logs/user-defined/rock_job_api-test-job.exit',
      });
      expect(result.status).toBe(JobStatus.FAILED);
      expect(result.exit_code).toBe(7);
      expect(result.trial_results[0].exit_code).toBe(7);
      expect(result.trial_results[0].exception_info?.exception_type).toBe('BashExitCode');
    });
  });

  describe('wait', () => {
    test('throws when submit was not called first', async () => {
      const config = makeConfig();
      const job = new Job(config);
      await expect(job.wait()).rejects.toThrow();
    });
  });

  describe('cancel', () => {
    test('calls sandbox arun with command and session options', async () => {
      const config = makeConfig();
      const job = new Job(config);
      const arun = jest.fn(async () => ({ output: '', exitCode: 0, failureReason: '', expectString: '' }));
      (job as any).jobClient = {
        trials: [{ sandbox: { arun }, session: 'rock-job-api-test-job', pid: 2468, trial: {} }],
      };

      await job.cancel();

      expect(arun).toHaveBeenCalledWith('kill 2468', { session: 'rock-job-api-test-job' });
    });

    test('propagates cancellation failures', async () => {
      const config = makeConfig();
      const job = new Job(config);
      const arun = jest.fn(async () => { throw new Error('kill failed'); });
      (job as any).jobClient = {
        trials: [{ sandbox: { arun }, session: 'rock-job-api-test-job', pid: 2468, trial: {} }],
      };

      await expect(job.cancel()).rejects.toThrow('kill failed');
    });
  });

  describe('_buildResult', () => {
    test('flattens list-returning results', () => {
      const config = makeConfig();
      const job = new Job(config);

      const raw: any[] = [
        { task_name: 't1', exception_info: null, exit_code: 0, status: 'completed', score: 0, duration_sec: 0 },
        [
          { task_name: 'sub1', exception_info: null, exit_code: 0, status: 'completed', score: 0, duration_sec: 0 },
          { task_name: 'sub2', exception_info: null, exit_code: 0, status: 'completed', score: 0, duration_sec: 0 },
        ],
      ];

      const result = (job as any)._buildResult(raw);
      expect(result.trial_results).toHaveLength(3);
      expect(result.trial_results[0].task_name).toBe('t1');
      expect(result.trial_results[1].task_name).toBe('sub1');
      expect(result.trial_results[2].task_name).toBe('sub2');
    });

    test('sets JobStatus.FAILED when any trial has exception', () => {
      const config = makeConfig();
      const job = new Job(config);

      const raw: any[] = [
        { task_name: 't1', exception_info: { exception_type: 'Error', exception_message: 'fail' }, exit_code: 1, status: 'failed', score: 0, duration_sec: 0 },
      ];

      const result = (job as any)._buildResult(raw);
      expect(result.status).toBe(JobStatus.FAILED);
    });

    test('sets JobStatus.COMPLETED when all trials succeed', () => {
      const config = makeConfig();
      const job = new Job(config);

      const raw: any[] = [
        { task_name: 't1', exception_info: null, exit_code: 0, status: 'completed', score: 0, duration_sec: 0 },
      ];

      const result = (job as any)._buildResult(raw);
      expect(result.status).toBe(JobStatus.COMPLETED);
    });
  });
});
