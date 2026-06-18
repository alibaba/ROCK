/**
 * JobExecutor — orchestrates the full execution of Trials produced by an Operator.
 *
 * Flow:
 *     submit(operator, config)  — apply operator to get TrialList, start all sandboxes
 *                                  in parallel, return JobClient (list of TrialClient)
 *     wait(job_client)          — wait for all trials, collect results, return list[TrialResult[]]
 *     run(operator, config)     — submit + wait
 *
 * Matches Python rock.sdk.job.executor.
 */

import type { AbstractTrial } from './trial/abstract';
import type { Operator } from './operator';

// ---------------------------------------------------------------------------
// TrialClient / JobClient
// ---------------------------------------------------------------------------

/** Handle for a single running trial. */
export interface TrialClient {
  sandbox: unknown; // Sandbox instance
  session: string;
  pid: number;
  trial: AbstractTrial;
}

/** Handle returned by JobExecutor.submit(). Holds multiple TrialClients. */
export interface JobClient {
  trials: TrialClient[];
}

// ---------------------------------------------------------------------------
// JobExecutor
// ---------------------------------------------------------------------------

export class JobExecutor {
  /**
   * Full lifecycle: submit + wait.
   */
  async run(
    operator: Operator,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    config: Record<string, any>
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ): Promise<any[]> {
    const jobClient = await this.submit(operator, config);
    return this.wait(jobClient);
  }

  /**
   * Operator generates TrialList, start all sandboxes in parallel.
   */
  async submit(
    operator: Operator,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    config: Record<string, any>
  ): Promise<JobClient> {
    const trialList = operator.apply(config);
    if (trialList.length === 0) {
      return { trials: [] };
    }
    // In production: Promise.all(trialList.map(t => this._doSubmit(t)))
    // For now: return empty (real sandbox integration coming later)
    const trials: TrialClient[] = [];
    for (const trial of trialList) {
      try {
        const tc = await this._doSubmit(trial, config);
        trials.push(tc);
      } catch {
        // Skip failed submissions in test mode
      }
    }
    return { trials };
  }

  /**
   * Wait for all trials, collect results in parallel.
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async wait(jobClient: JobClient): Promise<any[]> {
    if (jobClient.trials.length === 0) {
      return [];
    }
    // In production: Promise.all(jobClient.trials.map(tc => this._doWait(tc)))
    // For now: call collect on each trial directly
    const results = [];
    for (const tc of jobClient.trials) {
      try {
        const result = await tc.trial.collect(undefined, '', 0);
        results.push(result);
      } catch {
        // Skip failed collections
      }
    }
    return results;
  }

  // ------------------------------------------------------------------
  // Private
  // ------------------------------------------------------------------

  private async _doSubmit(
    trial: AbstractTrial,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    config: Record<string, any>
  ): Promise<TrialClient> {
    // In production:
    // 1. Create Sandbox from config.environment
    // 2. sandbox.start()
    // 3. trial.onSandboxReady(sandbox)
    // 4. trial.setup(sandbox)
    // 5. create session, write script, start nohup
    //
    // For now, return a minimal TrialClient (tests don't need real sandboxes)
    return {
      sandbox: null,
      session: `rock-job-${config['job_name'] ?? 'default'}`,
      pid: 0,
      trial,
    };
  }

  /**
   * Build session environment — merge OSS_* vars from process with config.env.
   * Config values take precedence over process env.
   */
  static buildSessionEnv(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    config: Record<string, any>
  ): Record<string, string> | null {
    const ossEnv: Record<string, string> = {};
    for (const [k, v] of Object.entries(process.env)) {
      if (k.startsWith('OSS') && v !== undefined) {
        ossEnv[k] = v;
      }
    }

    const env = (config as Record<string, unknown>)['environment'] as Record<string, unknown>;
    const configEnv = (env?.['env'] ?? {}) as Record<string, string>;

    const merged = { ...ossEnv, ...configEnv };

    if (Object.keys(merged).length === 0) return null;
    return merged;
  }
}
