/**
 * HarborTrial — execute a Harbor benchmark job inside a sandbox.
 *
 * Combines dockerd startup and ``harbor jobs start -c`` into a single bash
 * script executed by JobExecutor via the sandbox nohup protocol.
 *
 * Matches Python rock.sdk.job.trial.harbor.
 */

import type { TrialResult } from '../result';
import { ExceptionInfoSchema } from '../result';
import { AbstractTrial } from './abstract';
import { registerTrial } from './registry';
import { USER_DEFINED_LOGS } from '../../bench/constants';

/** Registry key for HarborJobConfig. */
export const HARBOR_JOB_CONFIG_KEY = Symbol.for('HarborJobConfig');

// ---------------------------------------------------------------------------
// Script template
// ---------------------------------------------------------------------------

const HARBOR_SCRIPT_TEMPLATE = `#!/bin/bash
set -e

# ── Detect and start dockerd ─────────────────────────────────────────
if command -v docker &>/dev/null; then
    echo "docker OK: $(command -v docker)"
    if ! pgrep -x dockerd &>/dev/null; then
        echo "Starting dockerd..."
        nohup dockerd &>/var/log/dockerd.log &
    fi
    for i in $(seq 1 60); do
        if docker info &>/dev/null; then echo "dockerd is ready"; break; fi
        sleep 1
        if [ "$i" -eq 60 ]; then echo "WARN: dockerd failed to start within 60s"; fi
    done
fi

# ── Ensure output directory exists ──────────────────────────────────
mkdir -p {user_defined_dir}

# ── Harbor run ───────────────────────────────────────────────────────
harbor jobs start -c {config_path}
`;

// ---------------------------------------------------------------------------
// HarborTrial
// ---------------------------------------------------------------------------

export class HarborTrial extends AbstractTrial {
  constructor(config: Record<string, unknown>) {
    super(config);
  }

  // ------------------------------------------------------------------
  // build
  // ------------------------------------------------------------------

  override build(): string {
    const configPath = `${USER_DEFINED_LOGS}/rock_job_${this.config['job_name'] ?? 'default'}.yaml`;
    return HARBOR_SCRIPT_TEMPLATE
      .replace('{config_path}', configPath)
      .replace('{user_defined_dir}', USER_DEFINED_LOGS);
  }

  // ------------------------------------------------------------------
  // collect
  // ------------------------------------------------------------------

  override async collect(
    _sandbox?: unknown,
    _output?: string,
    _exit_code?: number
  ): Promise<TrialResult[]> {
    // In production, this would:
    // 1. find result.json files in the job directory
    // 2. parse each with createHarborTrialResultFromJson
    // 3. return the parsed results
    //
    // For now, return a synthetic "no trials" result (matches Python behavior
    // when no trial result.json files are found).
    return [
      {
        task_name: (this.config['job_name'] as string) ?? '',
        exception_info: ExceptionInfoSchema.parse({
          exception_type: 'HarborNoTrials',
          exception_message: 'No trial results found',
        }),
        started_at: null,
        finished_at: null,
        raw_output: '',
        exit_code: 1,
        score: 0,
        status: 'failed',
        duration_sec: 0,
      },
    ];
  }
}

// Auto-register
registerTrial(HARBOR_JOB_CONFIG_KEY, HarborTrial);
