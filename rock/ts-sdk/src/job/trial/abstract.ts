/**
 * Trial abstract base class — three-phase interface (setup / build / collect).
 *
 * Trial objects do not manage sandbox lifecycle; that is handled by JobExecutor.
 * Matches Python rock.sdk.job.trial.abstract.AbstractTrial.
 */

import type { TrialResult } from '../result';
import type { Sandbox } from '../../sandbox/client';

// ---------------------------------------------------------------------------
// Sandbox-like interface (minimal, avoids circular deps)
// ---------------------------------------------------------------------------

/** Minimal interface for the sandbox bits AbstractTrial needs. */
export interface ISandbox {
  getNamespace(): string | null;
  getExperimentId(): string | null;
}

// ---------------------------------------------------------------------------
// AbstractTrial
// ---------------------------------------------------------------------------

/**
 * Base class for all trial types.
 *
 * Uses a loose config type to allow subclasses to narrow (e.g. BashTrial uses BashJobConfig).
 */
export abstract class AbstractTrial {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  readonly config: Record<string, any>;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  constructor(config: Record<string, any>) {
    this.config = config;
  }

  /**
   * G4 hook: called by JobExecutor once sandbox.start() succeeds, before setup().
   *
   * Default behavior backfills ``namespace`` and ``experiment_id`` from the
   * sandbox into ``config``. Subclasses can override to extend.
   */
  async onSandboxReady(sandbox: ISandbox): Promise<void> {
    // Backfill namespace
    const sbNs = sandbox.getNamespace();
    if (sbNs !== null && this.config.namespace === null) {
      this.config.namespace = sbNs;
    }

    // Backfill experiment_id — config value takes priority
    const sbExp = sandbox.getExperimentId();
    if (sbExp !== null && this.config.experiment_id === null) {
      this.config.experiment_id = sbExp;
    }
  }

  /**
   * Pre-execution: set up proxy (if enabled) and upload files.
   *
   * Subclasses should call ``await super.setup(sandbox)`` first, then add
   * their own setup logic.
   */
  async setup(_sandbox: Sandbox): Promise<void> {
    // Base setup: proxy and uploads — implemented in subclasses that need it.
    // The full Python version handles ModelService proxy start and file uploads.
    // For now, this is a no-op that subclasses can extend.
  }

  /**
   * Build: generate the bash script to execute.
   */
  abstract build(): string;

  /**
   * Post-execution: collect and parse results.
   *
   * Return a single ``TrialResult`` for one-shot tasks (e.g. BashTrial),
   * or a ``list[TrialResult]`` for multi-result tasks (e.g. HarborTrial).
   */
  abstract collect(
    sandbox?: Sandbox,
    output?: string,
    exit_code?: number
  ): Promise<TrialResult | TrialResult[]>;
}
