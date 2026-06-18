/**
 * BashTrial — execute a bash script inside a sandbox.
 *
 * Matches Python rock.sdk.job.trial.bash.
 */

import crypto from 'crypto';
import type { Sandbox } from '../../sandbox/client';
import type { BashJobConfig } from '../config';
import type { TrialResult } from '../result';
import { ExceptionInfoSchema } from '../result';
import { AbstractTrial } from './abstract';
import { registerTrial, _assignRegistryKey } from './registry';

/** Registry key for BashJobConfig. */
export const BASH_JOB_CONFIG_KEY = Symbol.for('BashJobConfig');

/** OSS credential fields to resolve. */
const OSS_CREDENTIAL_FIELDS = [
  'oss_access_key_id',
  'oss_access_key_secret',
  'oss_endpoint',
  'oss_region',
  'oss_bucket',
] as const;

/** Default artifact directory for bash jobs. */
const ROCK_BASH_JOB_ARTIFACT_DIR = '/data/logs/user-defined/artifacts';

// ---------------------------------------------------------------------------
// BashTrial
// ---------------------------------------------------------------------------

export class BashTrial extends AbstractTrial {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  declare readonly config: any;
  _ossutilReady: boolean = false;

  constructor(config: BashJobConfig) {
    super(config);
  }

  // ------------------------------------------------------------------
  // OSS mirror support
  // ------------------------------------------------------------------

  ossMirrorEnabled(): boolean {
    const env = this.config.environment as Record<string, unknown>;
    const mirror = env['oss_mirror'] as Record<string, unknown> | null;
    return mirror != null && mirror['enabled'] === true;
  }

  override async onSandboxReady(sandbox: {
    getNamespace(): string | null;
    getExperimentId(): string | null;
  }): Promise<void> {
    await super.onSandboxReady(sandbox);
    if (this.ossMirrorEnabled()) {
      this._prepareOssSessionEnv();
    }
  }

  _prepareOssSessionEnv(): void {
    const env = this.config.environment as Record<string, unknown>;
    const mirror = env['oss_mirror'] as Record<string, string | null | undefined>;
    const configEnv = (env['env'] ?? {}) as Record<string, string>;

    for (const fieldName of OSS_CREDENTIAL_FIELDS) {
      const envKey = fieldName.toUpperCase();
      const v =
        (mirror?.[fieldName] as string | undefined) ||
        configEnv[envKey] ||
        process.env[envKey];
      if (v) {
        configEnv[envKey] = v;
      }
    }

    if (!this.config.namespace) {
      throw new Error('oss_mirror: namespace is not set (sandbox did not return one)');
    }
    if (!this.config.experiment_id) {
      throw new Error('oss_mirror: experiment_id is not set (sandbox did not return one)');
    }
    for (const envKey of ['OSS_BUCKET', 'OSS_ENDPOINT', 'OSS_REGION']) {
      if (!configEnv[envKey]) {
        throw new Error(`oss_mirror.enabled=true but ${envKey} is not resolvable`);
      }
    }

    configEnv['ROCK_ARTIFACT_DIR'] = ROCK_BASH_JOB_ARTIFACT_DIR;
    configEnv['ROCK_OSS_PREFIX'] =
      `artifacts/${this.config.namespace}/${this.config.experiment_id}/${this.config.job_name}`;
  }

  // ------------------------------------------------------------------
  // Wrapper script rendering
  // ------------------------------------------------------------------

  /**
   * Render the BashJob wrapper script with heredoc isolation.
   *
   * Structure: prologue (mkdir + initial upload) -> user script (heredoc) ->
   * epilogue (final upload) -> exit with user's exit code.
   *
   * When `token` is `undefined` a random 8-char hex is generated.
   */
  static renderWrapper(userScript: string, token?: string): string {
    if (!token) {
      token = crypto.randomBytes(4).toString('hex'); // 8-char hex
    }
    const eof = `__ROCK_USER_SCRIPT_EOF_${token}__`;
    return (
      '#!/bin/bash\n' +
      '# rock bash-job wrapper (generated, do not edit)\n' +
      '# OSS credentials and paths come from session env; no secrets in this file.\n' +
      'set +e\n' +
      '\n' +
      '# -- prologue: prepare artifact dir and do an initial placeholder upload --\n' +
      'mkdir -p "$ROCK_ARTIFACT_DIR"\n' +
      'touch "$ROCK_ARTIFACT_DIR/.placeholder"\n' +
      'ossutil cp "$ROCK_ARTIFACT_DIR/" "oss://$OSS_BUCKET/$ROCK_OSS_PREFIX/" \\\n' +
      '    --recursive -f >/dev/null 2>&1 || true\n' +
      '\n' +
      '# -- user script: heredoc isolates user trap/exit from the wrapper --\n' +
      `bash <<'${eof}'\n` +
      `${userScript}\n` +
      `${eof}\n` +
      '_rock_user_rc=$?\n' +
      '\n' +
      '# -- epilogue: final upload (failure is logged but does not change exit code) --\n' +
      'ossutil cp "$ROCK_ARTIFACT_DIR/" "oss://$OSS_BUCKET/$ROCK_OSS_PREFIX/" \\\n' +
      '    --recursive -f \\\n' +
      '    || echo "[rock] oss upload failed (rc=$?), ignored" >&2\n' +
      '\n' +
      'exit $_rock_user_rc\n'
    );
  }

  // ------------------------------------------------------------------
  // setup
  // ------------------------------------------------------------------

  override async setup(_sandbox: Sandbox): Promise<void> {
    await super.setup(_sandbox);
    // In real usage: read script_path if set, ensure ossutil if OSS mirror enabled
  }

  // ------------------------------------------------------------------
  // build
  // ------------------------------------------------------------------

  build(): string {
    const script = this.config.script ?? '';
    if (!this.ossMirrorEnabled()) {
      return script;
    }
    if (!this._ossutilReady) {
      // ossutil unavailable — fall back to raw script
      return script;
    }
    return BashTrial.renderWrapper(script);
  }

  // ------------------------------------------------------------------
  // collect
  // ------------------------------------------------------------------

  async collect(
    _sandbox?: Sandbox,
    output?: string,
    exit_code?: number
  ): Promise<TrialResult> {
    const ec = exit_code ?? 0;
    let exceptionInfo = null;
    if (ec !== 0) {
      exceptionInfo = ExceptionInfoSchema.parse({
        exception_type: 'BashExitCode',
        exception_message: `Bash script exited with code ${ec}`,
      });
    }

    return {
      task_name: this.config.job_name ?? '',
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

// Auto-register on import (matching Python pattern)
registerTrial(BASH_JOB_CONFIG_KEY, BashTrial);
