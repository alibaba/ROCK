/**
 * Tests for job/result.ts — JobStatus, ExceptionInfo, TrialResult, JobResult
 */

import {
  JobStatus,
  ExceptionInfoSchema,
  ExceptionInfo,
  TrialResultSchema,
  TrialResult,
  JobResultSchema,
  JobResult,
} from './result';

// ---------------------------------------------------------------------------
// JobStatus
// ---------------------------------------------------------------------------
describe('JobStatus', () => {
  test('has all expected enum values', () => {
    expect(JobStatus.PENDING).toBe('pending');
    expect(JobStatus.RUNNING).toBe('running');
    expect(JobStatus.COMPLETED).toBe('completed');
    expect(JobStatus.FAILED).toBe('failed');
    expect(JobStatus.CANCELLED).toBe('cancelled');
  });
});

// ---------------------------------------------------------------------------
// ExceptionInfo
// ---------------------------------------------------------------------------
describe('ExceptionInfo', () => {
  describe('ExceptionInfoSchema', () => {
    test('parses empty object with defaults', () => {
      const result = ExceptionInfoSchema.parse({});
      expect(result.exception_type).toBe('');
      expect(result.exception_message).toBe('');
      expect(result.exception_traceback).toBe('');
      expect(result.occurred_at).toBeNull();
    });

    test('parses full exception info', () => {
      const result = ExceptionInfoSchema.parse({
        exception_type: 'RuntimeError',
        exception_message: 'Something went wrong',
        exception_traceback: 'Traceback...',
        occurred_at: '2024-01-01T00:00:00Z',
      });
      expect(result.exception_type).toBe('RuntimeError');
      expect(result.exception_message).toBe('Something went wrong');
      expect(result.exception_traceback).toBe('Traceback...');
      expect(result.occurred_at).toBe('2024-01-01T00:00:00Z');
    });
  });
});

// ---------------------------------------------------------------------------
// TrialResult
// ---------------------------------------------------------------------------
describe('TrialResult', () => {
  describe('TrialResultSchema', () => {
    test('parses empty object with defaults', () => {
      const result = TrialResultSchema.parse({});
      expect(result.task_name).toBe('');
      expect(result.exception_info).toBeNull();
      expect(result.started_at).toBeNull();
      expect(result.finished_at).toBeNull();
      expect(result.raw_output).toBe('');
      expect(result.exit_code).toBe(0);
    });

    test('computed score defaults to 0.0', () => {
      const result = TrialResultSchema.parse({});
      expect(result.score).toBe(0.0);
    });

    test('computed status is "completed" when no exception_info', () => {
      const result = TrialResultSchema.parse({});
      expect(result.status).toBe('completed');
    });

    test('computed status is "failed" when exception_info is present', () => {
      const result = TrialResultSchema.parse({
        exception_info: { exception_type: 'Error', exception_message: 'fail' },
      });
      expect(result.status).toBe('failed');
    });

    test('computed duration_sec is 0 when no timestamps', () => {
      const result = TrialResultSchema.parse({});
      expect(result.duration_sec).toBe(0);
    });

    test('computed duration_sec calculates correctly from ISO timestamps', () => {
      const result = TrialResultSchema.parse({
        started_at: '2024-01-01T00:00:00Z',
        finished_at: '2024-01-01T00:01:30Z',
      });
      expect(result.duration_sec).toBe(90);
    });

    test('parses full trial result with exit code and raw output', () => {
      const result = TrialResultSchema.parse({
        task_name: 'my-task',
        raw_output: 'hello world',
        exit_code: 0,
      });
      expect(result.task_name).toBe('my-task');
      expect(result.raw_output).toBe('hello world');
      expect(result.exit_code).toBe(0);
      expect(result.status).toBe('completed');
    });

    test('parses failed trial result', () => {
      const result = TrialResultSchema.parse({
        task_name: 'failed-task',
        exception_info: {
          exception_type: 'BashExitCode',
          exception_message: 'Bash script exited with code 1',
        },
        raw_output: 'error output',
        exit_code: 1,
      });
      expect(result.task_name).toBe('failed-task');
      expect(result.status).toBe('failed');
      expect(result.exception_info?.exception_type).toBe('BashExitCode');
      expect(result.exit_code).toBe(1);
    });
  });
});

// ---------------------------------------------------------------------------
// JobResult
// ---------------------------------------------------------------------------
describe('JobResult', () => {
  const jobResultSchema = JobResultSchema(TrialResultSchema);

  describe('JobResultSchema', () => {
    test('parses empty object with defaults', () => {
      const result = jobResultSchema.parse({});
      expect(result.job_id).toBe('');
      expect(result.status).toBe(JobStatus.COMPLETED);
      expect(result.labels).toEqual({});
      expect(result.trial_results).toEqual([]);
      expect(result.raw_output).toBe('');
      expect(result.exit_code).toBe(0);
    });

    test('computed score is 0 when no trial results', () => {
      const result = jobResultSchema.parse({});
      expect(result.score).toBe(0.0);
    });

    test('computed score averages trial result scores', () => {
      const result = jobResultSchema.parse({
        job_id: 'test-job',
        trial_results: [
          { task_name: 'task-1', exit_code: 0 },
          { task_name: 'task-2', exit_code: 0 },
        ],
      });
      // Both default to score 0.0, so avg is 0.0
      expect(result.score).toBe(0.0);
    });

    test('computed n_completed counts completed trials', () => {
      const result = jobResultSchema.parse({
        trial_results: [
          { task_name: 'ok-1' },
          { task_name: 'failed', exception_info: { exception_type: 'E', exception_message: 'm' } },
        ],
      });
      expect(result.n_completed).toBe(1);
      expect(result.n_failed).toBe(1);
    });

    test('parses with job_id, status, labels', () => {
      const result = jobResultSchema.parse({
        job_id: 'my-job',
        status: JobStatus.FAILED,
        labels: { env: 'test' },
        raw_output: 'some output',
        exit_code: 1,
      });
      expect(result.job_id).toBe('my-job');
      expect(result.status).toBe(JobStatus.FAILED);
      expect(result.labels).toEqual({ env: 'test' });
      expect(result.raw_output).toBe('some output');
      expect(result.exit_code).toBe(1);
    });

    test('accepts and stores trial results', () => {
      const result = jobResultSchema.parse({
        trial_results: [
          { task_name: 't1', raw_output: 'out1', exit_code: 0 },
          { task_name: 't2', raw_output: 'out2', exit_code: 1, exception_info: { exception_type: 'E', exception_message: 'm' } },
        ],
      });
      expect(result.trial_results).toHaveLength(2);
      expect(result.trial_results[0]!.task_name).toBe('t1');
      expect(result.trial_results[1]!.exit_code).toBe(1);
      expect(result.n_completed).toBe(1);
      expect(result.n_failed).toBe(1);
    });
  });
});
