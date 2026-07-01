/**
 * Abstract base class for speedup strategies
 *
 * Mirrors Python rock/sdk/sandbox/speedup/base.py
 */

import type { AbstractSandbox } from '../client.js'; // eslint-disable-line @typescript-eslint/no-unused-vars -- used in JSDoc type references, actual typing via subclass

/**
 * Result of a precheck operation
 */
export interface PrecheckResult {
  /** Whether the precheck passed */
  passed: boolean;
  /** Descriptive message about the check result */
  message: string;
}

/**
 * Speedup strategy abstract base class
 *
 * Each strategy handles one SpeedupType (APT, PIP, GITHUB, etc.).
 * Subclasses must implement precheck(), generateScript(), and parseValue().
 */
export abstract class SpeedupStrategy {
  /**
   * Precheck if environment meets requirements
   *
   * @param sandbox - Sandbox instance (used to run lightweight command checks)
   * @returns Tuple of (check passed, check message)
   */
  abstract precheck(sandbox: AbstractSandbox): Promise<PrecheckResult>;

  /**
   * Generate speedup configuration script
   *
   * @param speedupValue - Speedup value (mirror URL, IP address, etc.)
   * @returns Script content as a bash script string
   */
  abstract generateScript(speedupValue: string): string;

  /**
   * Parse speedup value and extract required parameters
   *
   * @param speedupValue - Speedup value string
   * @returns Parameters for template filling
   */
  abstract parseValue(speedupValue: string): Record<string, string>;

  /**
   * Get nohup wait timeout in seconds
   *
   * @returns Timeout value (default 30s)
   */
  getNohupWaitTimeout(): number {
    return 30;
  }
}
