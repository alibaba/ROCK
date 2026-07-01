/**
 * Operator — generic algorithm that produces a TrialList from a JobConfig.
 *
 * Matches Python rock.sdk.job.operator.
 */

import type { AbstractTrial } from './trial/abstract';
import { createTrial } from './trial/registry';

// ---------------------------------------------------------------------------
// Operator interface
// ---------------------------------------------------------------------------

/**
 * Operator base: apply(config) -> list[AbstractTrial].
 *
 * Operators generate a TrialList from a config. They don't manage
 * sandbox lifecycle (JobExecutor does) — just decide what to run.
 */
export interface Operator {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  apply(config: Record<string, any>): AbstractTrial[];
}

// ---------------------------------------------------------------------------
// ScatterOperator
// ---------------------------------------------------------------------------

/**
 * Scatter: create `size` identical Trial instances from config.
 *
 * Analog of torch.distributed.scatter — same data/config distributed to N workers.
 *
 * Usage:
 *   new ScatterOperator()      // size=1, single trial (default)
 *   new ScatterOperator(8)     // 8 parallel trials
 *   new ScatterOperator(0)     // empty list, no-op
 */
export class ScatterOperator implements Operator {
  readonly size: number;

  constructor(size: number = 1) {
    this.size = size;
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  apply(config: Record<string, any>): AbstractTrial[] {
    if (this.size <= 0) return [];
    const trial = createTrial(config);
    return Array(this.size).fill(trial);
  }
}
