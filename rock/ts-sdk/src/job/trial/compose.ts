/**
 * ComposeTrial — multi-container job execution via Docker Compose in a DinD sandbox.
 *
 * Lifecycle:
 *     setup()   — size the sandbox, upload user files
 *     build()   — generate runner.sh (dockerd + compose lifecycle)
 *     collect() — parse result.json from the sandbox
 *
 * Matches Python rock.sdk.job.trial.compose.
 */

import type { TrialResult } from '../result';
import { ExceptionInfoSchema } from '../result';
import { AbstractTrial } from './abstract';
import { registerTrial } from './registry';
import { buildRunnerScript } from '../compose/script_builder';
import type { ComposeJobConfig } from '../config_compose';

/** Registry key for ComposeJobConfig. */
export const COMPOSE_JOB_CONFIG_KEY = Symbol.for('ComposeJobConfig');

// ---------------------------------------------------------------------------
// Exit code helpers
// ---------------------------------------------------------------------------

function _exitCodeToType(code: number): string {
  if (code === 90) return 'DockerdStartupTimeout';
  if (code === 91) return 'ComposeUpFailed';
  if (code === 92) return 'InitContainerFailed';
  return 'ComposeExitCode';
}

function _exitCodeToMessage(code: number, output: string): string {
  if (code === 90) return 'dockerd failed to start within 120s';
  if (code === 91) return 'docker compose up -d failed';
  if (code === 92) return 'init container failed (serial execution aborted)';
  const tail = output.length > 500 ? output.slice(-500) : output;
  return `Compose job exited with code ${code}. Tail output: ${tail}`;
}

// ---------------------------------------------------------------------------
// ComposeTrial
// ---------------------------------------------------------------------------

export class ComposeTrial extends AbstractTrial {
  constructor(config: ComposeJobConfig) {
    super(config);
  }

  // ------------------------------------------------------------------
  // build
  // ------------------------------------------------------------------

  override build(): string {
    return buildRunnerScript(this.config as unknown as ComposeJobConfig);
  }

  // ------------------------------------------------------------------
  // collect
  // ------------------------------------------------------------------

  override async collect(
    _sandbox?: unknown,
    output?: string,
    exit_code?: number
  ): Promise<TrialResult> {
    const ec = exit_code ?? 0;
    let exceptionInfo = null;
    if (ec !== 0) {
      const excType = _exitCodeToType(ec);
      const excMsg = _exitCodeToMessage(ec, output ?? '');
      exceptionInfo = ExceptionInfoSchema.parse({
        exception_type: excType,
        exception_message: excMsg,
      });
    }

    return {
      task_name: (this.config['job_name'] as string) ?? '',
      exception_info: exceptionInfo,
      started_at: null,
      finished_at: null,
      raw_output: output ?? '',
      exit_code: ec,
      score: 0,
      status: ec === 0 ? 'completed' : 'failed',
      duration_sec: 0,
    };
  }
}

// Auto-register
registerTrial(COMPOSE_JOB_CONFIG_KEY, ComposeTrial);
