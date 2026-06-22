/**
 * Job — thin user-facing facade over JobExecutor + Operator.
 *
 * Only 2 params (config + operator). Delegates everything to JobExecutor.
 *
 * Usage:
 *     const result = await new Job(config).run()
 *     // or
 *     const job = new Job(config, new ScatterOperator(8))
 *     await job.submit()
 *     const result = await job.wait()
 *
 * Matches Python rock.sdk.job.api.
 */

import { JobExecutor, JobClient } from './executor';
import { ScatterOperator, Operator } from './operator';
import { JobStatus, TrialResult } from './result';

export class Job {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private config: Record<string, any>;
  private executor: JobExecutor;
  private operator: Operator;
  private jobClient: JobClient | null = null;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  constructor(config: Record<string, any>, operator?: Operator) {
    this.config = config;
    this.executor = new JobExecutor();
    this.operator = operator ?? new ScatterOperator();
  }

  /**
   * Full lifecycle: submit + wait.
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async run(): Promise<any> {
    await this.submit();
    return this.wait();
  }

  /**
   * Non-blocking submit: operator generates trials, executor starts them.
   */
  async submit(): Promise<void> {
    this.jobClient = await this.executor.submit(this.operator, this.config);
  }

  /**
   * Wait for completion, build JobResult.
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async wait(): Promise<any> {
    if (!this.jobClient) {
      throw new Error('No submitted job. Call submit() first.');
    }
    const raw = await this.executor.wait(this.jobClient);
    return this._buildResult(raw);
  }

  /**
   * Cancel all running trials.
   */
  async cancel(): Promise<void> {
    if (this.jobClient) {
      for (const tc of this.jobClient.trials) {
        const sandbox = tc.sandbox as { arun?: (cmd: string, options?: { session?: string }) => Promise<unknown> };
        if (sandbox?.arun) {
          await sandbox.arun(`kill ${tc.pid}`, { session: tc.session });
        }
      }
    }
  }

  /**
   * Flatten list-returning collect() outputs into JobResult.trial_results.
   *
   * Each element of ``rawResults`` is whatever one Trial's ``collect()``
   * returned — either a single TrialResult or a list. HarborTrial returns
   * a list (one entry per sub-trial); BashTrial returns a single result.
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  _buildResult(rawResults: any[]): any {
    const flat: TrialResult[] = [];
    for (const r of rawResults) {
      if (Array.isArray(r)) {
        flat.push(...r);
      } else {
        flat.push(r);
      }
    }

    const allSuccess = flat.every((t) => t.exception_info === null);

    // G5: surface first non-empty output / non-zero exit code from sub-trials
    let rawOutput = '';
    let exitCode = 0;
    for (const t of flat) {
      if (t.raw_output && !rawOutput) rawOutput = t.raw_output;
      if (t.exit_code !== 0 && exitCode === 0) exitCode = t.exit_code;
    }

    return {
      job_id: this.config['job_name'] ?? '',
      status: allSuccess ? JobStatus.COMPLETED : JobStatus.FAILED,
      labels: this.config['labels'] ?? {},
      trial_results: flat,
      raw_output: rawOutput,
      exit_code: exitCode,
    };
  }
}
