/**
 * Harbor trial result models — aligned with rock.sdk.bench.models.trial.result
 *
 * HarborTrialResult extends the base TrialResult (from rock.sdk.job.result),
 * adding agent_info, verifier_result, and timing sub-models.
 *
 * ExceptionInfo is re-exported from job/result.ts for backward compatibility,
 * but the local schema is used internally to avoid Zod cross-module inference issues.
 */

import { z } from 'zod';

// ExceptionInfo is defined locally but has the same shape as job/result.ts.
// (Cross-module Zod schema imports degrade type inference in ts-jest, so we
// keep the definition local and re-export the type for external consumers.)
export const ExceptionInfoSchema = z.object({
  exception_type: z.string().default(''),
  exception_message: z.string().default(''),
  exception_traceback: z.string().default(''),
  occurred_at: z.string().nullable().default(null),
});

export type ExceptionInfo = z.infer<typeof ExceptionInfoSchema>;

// ---------------------------------------------------------------------------
// ModelInfo
// ---------------------------------------------------------------------------

export const ModelInfoSchema = z.object({
  name: z.string().default(''),
  provider: z.string().default(''),
});

export type ModelInfo = z.infer<typeof ModelInfoSchema>;

// ---------------------------------------------------------------------------
// AgentInfo
// ---------------------------------------------------------------------------

export const AgentInfoSchema = z.object({
  name: z.string().default(''),
  version: z.string().default(''),
  model_info: ModelInfoSchema.nullable().default(null),
});

export type AgentInfo = z.infer<typeof AgentInfoSchema>;

// ---------------------------------------------------------------------------
// AgentResult
// ---------------------------------------------------------------------------

export const AgentResultSchema = z.object({
  n_input_tokens: z.number().int().nullable().default(null),
  n_cache_tokens: z.number().int().nullable().default(null),
  n_output_tokens: z.number().int().nullable().default(null),
  cost_usd: z.number().nullable().default(null),
  rollout_details: z.array(z.record(z.unknown())).nullable().default(null),
});

export type AgentResult = z.infer<typeof AgentResultSchema>;

// ---------------------------------------------------------------------------
// VerifierResult
// ---------------------------------------------------------------------------

export const VerifierResultSchema = z.object({
  rewards: z.record(z.union([z.number(), z.number().int()])).nullable().default(null),
});

export type VerifierResult = z.infer<typeof VerifierResultSchema>;

// ---------------------------------------------------------------------------
// TimingInfo
// ---------------------------------------------------------------------------

export const TimingInfoSchema = z.object({
  started_at: z.string().nullable().default(null),
  finished_at: z.string().nullable().default(null),
});

export type TimingInfo = z.infer<typeof TimingInfoSchema>;

// ---------------------------------------------------------------------------
// HarborTrialResult
//
// Base fields (task_name, exception_info, started_at, finished_at, raw_output,
// exit_code) match those in job/result.ts TrialResult.
//
// Computed properties (score, status, token_ids, duration_sec) are added
// via a zod transform().
// ---------------------------------------------------------------------------

/** Internal payload type — all data fields without computed properties. */
const _HarborTrialResultPayloadSchema = z.object({
  // ---- Base TrialResult fields ----
  task_name: z.string().default(''),
  exception_info: ExceptionInfoSchema.nullable().default(null),
  started_at: z.string().nullable().default(null),
  finished_at: z.string().nullable().default(null),
  raw_output: z.string().default(''),
  exit_code: z.number().int().default(0),

  // ---- Harbor-specific fields ----
  trial_name: z.string().default(''),
  source: z.string().nullable().default(null),
  agent_info: AgentInfoSchema.default({}),
  agent_result: AgentResultSchema.nullable().default(null),
  verifier_result: VerifierResultSchema.nullable().default(null),
  environment_setup: TimingInfoSchema.nullable().default(null),
  agent_setup: TimingInfoSchema.nullable().default(null),
  agent_execution: TimingInfoSchema.nullable().default(null),
  verifier: TimingInfoSchema.nullable().default(null),
});

type _Payload = z.infer<typeof _HarborTrialResultPayloadSchema>;

export type HarborTrialResult = _Payload & {
  readonly score: number;
  readonly status: string;
  readonly token_ids: number[];
  readonly duration_sec: number;
};

function _addComputed(payload: _Payload): HarborTrialResult {
  return Object.defineProperties(payload, {
    score: {
      get(this: _Payload): number {
        if (this.verifier_result?.rewards) {
          const reward = this.verifier_result.rewards['reward'];
          return typeof reward === 'number' ? reward : 0.0;
        }
        return 0.0;
      },
      enumerable: true,
      configurable: true,
    },
    status: {
      get(this: _Payload): string {
        return this.exception_info ? 'failed' : 'completed';
      },
      enumerable: true,
      configurable: true,
    },
    token_ids: {
      get(this: _Payload): number[] {
        if (this.agent_result?.rollout_details) {
          const ids: number[] = [];
          for (const detail of this.agent_result.rollout_details) {
            const tokenIds = detail['completion_token_ids'];
            if (Array.isArray(tokenIds)) {
              ids.push(...(tokenIds as number[]));
            }
          }
          return ids;
        }
        return [];
      },
      enumerable: true,
      configurable: true,
    },
    duration_sec: {
      get(this: _Payload): number {
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
  }) as HarborTrialResult;
}

export const HarborTrialResultSchema = _HarborTrialResultPayloadSchema.transform((payload) =>
  _addComputed(payload)
);

/**
 * Parse a harbor trial-level result.json dict into HarborTrialResult.
 */
export function createHarborTrialResultFromJson(
  data: Record<string, unknown>
): HarborTrialResult {
  const normalized = { ...data };
  if (normalized['exception_info']) {
    const ei = normalized['exception_info'];
    if (typeof ei === 'string') {
      normalized['exception_info'] = {
        exception_type: 'unknown',
        exception_message: ei,
      };
    }
  }

  return HarborTrialResultSchema.parse(normalized);
}
