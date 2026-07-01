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
import { USER_DEFINED_LOGS } from '../bench/constants';
import { Sandbox } from '../sandbox/client';
import { ExceptionInfoSchema, type TrialResult } from './result';
import type { Observation, ReadFileResponse } from '../types/responses';
import type { CreateBashSessionRequest, WriteFileRequest, ReadFileRequest } from '../types/requests';
import { shellQuote } from '../utils/shell';

// ---------------------------------------------------------------------------
// TrialClient / JobClient
// ---------------------------------------------------------------------------

/** Handle for a single running trial. */
export interface TrialClient {
  sandbox: JobSandbox;
  session: string;
  pid: number;
  trial: AbstractTrial;
}

/** Handle returned by JobExecutor.submit(). Holds multiple TrialClients. */
export interface JobClient {
  trials: TrialClient[];
}

export interface JobSandbox {
  start(): Promise<void>;
  getNamespace(): string | null;
  getExperimentId(): string | null;
  createSession(request: CreateBashSessionRequest): Promise<unknown>;
  writeFile(request: WriteFileRequest): Promise<{ success: boolean; message?: string }>;
  readFile(request: ReadFileRequest): Promise<ReadFileResponse>;
  startNohupProcess(
    cmd: string,
    tmpFile: string,
    session: string
  ): Promise<{ pid: number | null; errorResponse: Observation | null }>;
  waitForProcessCompletion(
    pid: number,
    session: string,
    waitTimeout: number,
    waitInterval: number
  ): Promise<{ success: boolean; message: string }>;
  handleNohupOutput(
    tmpFile: string,
    session: string,
    success: boolean,
    message: string,
    ignoreOutput: boolean,
    responseLimitedBytesInNohup: number | null
  ): Promise<Observation>;
  arun?(cmd: string, options?: { session?: string }): Promise<unknown>;
}

export type SandboxFactory = (config: Record<string, unknown>) => JobSandbox;

// ---------------------------------------------------------------------------
// JobExecutor
// ---------------------------------------------------------------------------

export class JobExecutor {
  constructor(
    private readonly sandboxFactory: SandboxFactory = (config) => new Sandbox(config)
  ) {}

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
    const trials = await Promise.all(trialList.map((trial) => this._doSubmit(trial)));
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
    return Promise.all(jobClient.trials.map((tc) => this._doWait(tc)));
  }

  // ------------------------------------------------------------------
  // Private
  // ------------------------------------------------------------------

  private static jobTmpPrefix(config: Record<string, unknown>): string {
    return `${USER_DEFINED_LOGS}/rock_job_${config['job_name'] ?? 'default'}`;
  }

  private static jobExitPath(config: Record<string, unknown>): string {
    return `${JobExecutor.jobTmpPrefix(config)}.exit`;
  }

  private static buildRunScriptCommand(scriptPath: string, exitPath: string): string {
    const inner = [
      `bash ${shellQuote(scriptPath)}`,
      'rc=$?',
      `echo "$rc" > ${shellQuote(exitPath)}`,
      'exit "$rc"',
    ].join('; ');
    return `bash -c ${shellQuote(inner)}`;
  }

  private async _doSubmit(trial: AbstractTrial): Promise<TrialClient> {
    const config = trial.config;
    const sandbox = this.sandboxFactory((config['environment'] ?? {}) as Record<string, unknown>);

    await sandbox.start();
    await trial.onSandboxReady(sandbox);
    await trial.setup(sandbox as Sandbox);

    const session = `rock-job-${config['job_name'] ?? 'default'}`;
    const env = JobExecutor.buildSessionEnv(config);
    await sandbox.createSession({ session, startupSource: [], envEnable: true, env: env ?? undefined });

    const scriptPath = `${JobExecutor.jobTmpPrefix(config)}.sh`;
    const writeResult = await sandbox.writeFile({ content: trial.build(), path: scriptPath });
    if (!writeResult.success) {
      throw new Error(`Failed to write job script ${scriptPath}: ${writeResult.message ?? ''}`);
    }

    const tmpFile = `${JobExecutor.jobTmpPrefix(config)}.out`;
    const exitPath = JobExecutor.jobExitPath(config);
    const { pid, errorResponse } = await sandbox.startNohupProcess(
      JobExecutor.buildRunScriptCommand(scriptPath, exitPath),
      tmpFile,
      session
    );
    if (errorResponse) {
      throw new Error(`Failed to start trial: ${errorResponse.output || errorResponse.failureReason}`);
    }
    if (!pid) {
      throw new Error('Failed to start trial: nohup did not return a PID');
    }

    return { sandbox, session, pid, trial };
  }

  private async _doWait(client: TrialClient): Promise<TrialResult | TrialResult[]> {
    const config = client.trial.config;
    const { success, message } = await client.sandbox.waitForProcessCompletion(
      client.pid,
      client.session,
      config['timeout'] ?? 7200,
      30
    );
    const obs = await client.sandbox.handleNohupOutput(
      `${JobExecutor.jobTmpPrefix(config)}.out`,
      client.session,
      success,
      message,
      false,
      null
    );
    const exitCode = await this.readScriptExitCode(client, obs, success);
    const result = await client.trial.collect(client.sandbox as Sandbox, obs.output ?? '', exitCode);
    const results = Array.isArray(result) ? result : [result];

    for (const r of results) {
      if (!r.raw_output) {
        r.raw_output = obs.output ?? '';
      }
      if (r.exit_code === 0 && exitCode !== 0) {
        r.exit_code = exitCode;
      }
      if (!success && r.exception_info === null) {
        r.exception_info = ExceptionInfoSchema.parse({
          exception_type: 'ProcessTimeout',
          exception_message: message || 'process did not complete successfully',
        });
      }
    }

    return result;
  }

  private async readScriptExitCode(
    client: TrialClient,
    obs: Observation,
    waitSuccess: boolean
  ): Promise<number> {
    try {
      const response = await client.sandbox.readFile({
        path: JobExecutor.jobExitPath(client.trial.config),
      });
      const parsed = Number.parseInt(response.content.trim(), 10);
      if (Number.isInteger(parsed)) {
        return parsed;
      }
    } catch {
      // Fall back to the nohup protocol status for older or partially failed jobs.
    }
    return obs.exitCode ?? (waitSuccess ? 0 : 1);
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
