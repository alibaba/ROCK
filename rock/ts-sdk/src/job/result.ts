/**
 * Result models for the Job system.
 *
 * Base classes: TrialResult, JobStatus, JobResult.
 * Harbor-specific subclasses in bench/models/trial/result.ts extend these.
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// JobStatus
// ---------------------------------------------------------------------------

/** Job status enum — matches Python rock.sdk.job.result.JobStatus. */
export enum JobStatus {
  PENDING = 'pending',
  RUNNING = 'running',
  COMPLETED = 'completed',
  FAILED = 'failed',
  CANCELLED = 'cancelled',
}

// ---------------------------------------------------------------------------
// ExceptionInfo
// ---------------------------------------------------------------------------

export const ExceptionInfoSchema = z.object({
  exception_type: z.string().default(''),
  exception_message: z.string().default(''),
  exception_traceback: z.string().default(''),
  occurred_at: z.string().nullable().default(null),
});

export type ExceptionInfo = z.infer<typeof ExceptionInfoSchema>;

// ---------------------------------------------------------------------------
// TrialResult
// ---------------------------------------------------------------------------

const _TrialResultPayloadSchema = z.object({
  task_name: z.string().default(''),
  exception_info: ExceptionInfoSchema.nullable().default(null),
  started_at: z.string().nullable().default(null),
  finished_at: z.string().nullable().default(null),
  raw_output: z.string().default(''),
  exit_code: z.number().int().default(0),
});

type _TrialResultPayload = z.infer<typeof _TrialResultPayloadSchema>;

/**
 * TrialResult — base class for a single execution result.
 *
 * Computed getters (score, status, duration_sec) are attached via a transform
 * so they work on any parsed data without a class wrapper.
 */
export type TrialResult = _TrialResultPayload & {
  readonly score: number;
  readonly status: string;
  readonly duration_sec: number;
};

function _addTrialResultComputed(payload: _TrialResultPayload): TrialResult {
  return Object.defineProperties(payload, {
    score: {
      get(): number {
        return 0.0;
      },
      enumerable: true,
      configurable: true,
    },
    status: {
      get(): string {
        return this.exception_info ? 'failed' : 'completed';
      },
      enumerable: true,
      configurable: true,
    },
    duration_sec: {
      get(): number {
        if (this.started_at && this.finished_at) {
          try {
            const start = new Date(this.started_at.replace('Z', '+00:00')).getTime();
            const end = new Date(this.finished_at.replace('Z', '+00:00')).getTime();
            return (end - start) / 1000;
          } catch {
            return 0.0;
          }
        }
        return 0.0;
      },
      enumerable: true,
      configurable: true,
    },
  }) as TrialResult;
}

/** Zod schema for TrialResult: validates data and attaches computed getters. */
export const TrialResultSchema = _TrialResultPayloadSchema.transform((payload) =>
  _addTrialResultComputed(payload)
);

// ---------------------------------------------------------------------------
// JobResult
// ---------------------------------------------------------------------------

/**
 * JobResult — aggregated result of a complete job run.
 *
 * Generic over trial result type:
 *   - JobResult<TrialResult>       — base (new Job system)
 *   - JobResult<HarborTrialResult> — Harbor agent system
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function _JobResultPayloadSchema<T extends z.ZodTypeAny>(trialResultSchema: T) {
  return z.object({
    job_id: z.string().default(''),
    status: z.nativeEnum(JobStatus).default(JobStatus.COMPLETED),
    labels: z.record(z.string()).default({}),
    trial_results: z.array(trialResultSchema).default([]),
    raw_output: z.string().default(''),
    exit_code: z.number().int().default(0),
  });
}

type _JobResultPayload<T> = {
  job_id: string;
  status: JobStatus;
  labels: Record<string, string>;
  trial_results: T[];
  raw_output: string;
  exit_code: number;
};

/**
 * JobResult type — the payload plus computed getters for score, n_completed, n_failed.
 */
export type JobResult<T extends { score: number; status: string } = TrialResult> =
  _JobResultPayload<T> & {
    readonly score: number;
    readonly n_completed: number;
    readonly n_failed: number;
  };

function _addJobResultComputed<T extends { score: number; status: string }>(
  payload: _JobResultPayload<T>
): JobResult<T> {
  return Object.defineProperties(payload, {
    score: {
      get(): number {
        if (this.trial_results.length === 0) return 0.0;
        let total = 0;
        for (const t of this.trial_results) {
          total += t.score;
        }
        return total / this.trial_results.length;
      },
      enumerable: true,
      configurable: true,
    },
    n_completed: {
      get(): number {
        let n = 0;
        for (const t of this.trial_results) {
          if (t.status === 'completed') n++;
        }
        return n;
      },
      enumerable: true,
      configurable: true,
    },
    n_failed: {
      get(): number {
        let n = 0;
        for (const t of this.trial_results) {
          if (t.status === 'failed') n++;
        }
        return n;
      },
      enumerable: true,
      configurable: true,
    },
  }) as JobResult<T>;
}

/**
 * Create a JobResultSchema for a given trial result schema.
 *
 * Usage: `const schema = JobResultSchema(TrialResultSchema);`
 */
export function JobResultSchema<T extends z.ZodTypeAny>(trialResultSchema: T) {
  return _JobResultPayloadSchema(trialResultSchema).transform((payload) =>
    _addJobResultComputed(payload)
  );
}
