/**
 * Speedup executor for coordinating speedup operations
 *
 * Mirrors Python rock/sdk/sandbox/speedup/executor.py
 */

import { initLogger } from '../../logger.js';
import type { Observation } from '../../types/responses.js';
import type { Sandbox } from '../client.js';
import type { Process } from '../process.js';
import { SpeedupStrategy } from './base.js';
import type { PrecheckResult } from './base.js';
import { AptSpeedupStrategy } from './strategies/apt.js';
import { PipSpeedupStrategy } from './strategies/pip.js';
import { GithubSpeedupStrategy } from './strategies/github.js';
import { SpeedupType } from './types.js';

const logger = initLogger('rock.sandbox.speedup.executor');

/**
 * Speedup executor (coordinator)
 *
 * Implements the template method pattern:
 *   1. Get strategy from registry
 *   2. Precheck environment
 *   3. Generate script
 *   4. Execute script via sandbox process
 */
export class SpeedupExecutor {
  /** Strategy registry — maps SpeedupType to strategy constructor */
  private static strategies: Map<SpeedupType, new () => SpeedupStrategy> = new Map([
    [SpeedupType.APT, AptSpeedupStrategy],
    [SpeedupType.PIP, PipSpeedupStrategy],
    [SpeedupType.GITHUB, GithubSpeedupStrategy],
  ]);

  private sandbox: Sandbox;
  private process: Process;

  constructor(sandbox: Sandbox) {
    this.sandbox = sandbox;
    this.process = sandbox.getProcess();
  }

  /**
   * Register a new speedup strategy
   *
   * @param speedupType - Speedup type to register
   * @param strategyClass - Strategy class constructor
   */
  static registerStrategy(speedupType: SpeedupType, strategyClass: new () => SpeedupStrategy): void {
    SpeedupExecutor.strategies.set(speedupType, strategyClass);
    logger.info(`Registered speedup strategy: ${speedupType} -> ${strategyClass.name}`);
  }

  /**
   * Execute speedup configuration (template method pattern)
   *
   * @param speedupType - Speedup type (APT, PIP, GITHUB, etc.)
   * @param speedupValue - Speedup value string (mirror URL, IP address, etc.)
   * @param timeout - Execution timeout in seconds (default 300)
   * @returns Observation with execution result
   */
  async execute(
    speedupType: SpeedupType,
    speedupValue: string,
    timeout: number = 300
  ): Promise<Observation> {
    const sandboxId = this.sandbox.getSandboxId();
    logger.info(`[${sandboxId}] Starting speedup: type=${speedupType}, value=${speedupValue}, timeout=${timeout}`);

    // 1. Get strategy
    const strategy = this.getStrategy(speedupType);
    if (!strategy) {
      const errorMsg = `Unsupported speedup type: ${speedupType}`;
      logger.error(errorMsg);
      return { output: errorMsg, exitCode: 1, failureReason: 'Invalid speedup type', expectString: '' };
    }

    // 2. Precheck environment
    const { passed, message } = await this.precheck(strategy);
    if (!passed) {
      logger.warn(`[${sandboxId}] Precheck failed: ${message}`);
      return { output: message, exitCode: 1, failureReason: 'Precheck failed', expectString: '' };
    }

    logger.info(`[${sandboxId}] Precheck passed: ${message}`);

    // 3. Generate script
    let scriptContent: string | null;
    try {
      scriptContent = strategy.generateScript(speedupValue);
      if (!scriptContent) {
        const errorMsg = 'Failed to generate speedup script';
        logger.error(errorMsg);
        return { output: errorMsg, exitCode: 1, failureReason: 'Script generation failed', expectString: '' };
      }
    } catch (e) {
      const errorMsg = `Failed to generate speedup script: ${String(e)}`;
      logger.error(errorMsg);
      return { output: errorMsg, exitCode: 1, failureReason: 'Script generation failed', expectString: '' };
    }

    // 4. Execute script using the general executeScript method
    const result = await this.process.executeScript({
      scriptContent,
      waitTimeout: timeout,
      cleanup: true,
    });

    // 5. Log result
    if (result.exitCode === 0) {
      logger.info(
        `[${sandboxId}] Speedup completed successfully: type=${speedupType}, output_length=${result.output.length}`
      );
    } else {
      logger.error(
        `[${sandboxId}] Speedup failed: type=${speedupType}, exit_code=${result.exitCode}, ` +
          `failure_reason=${result.failureReason}`
      );
    }

    return result;
  }

  /**
   * Get strategy instance by type
   */
  private getStrategy(speedupType: SpeedupType): SpeedupStrategy | null {
    const strategyClass = SpeedupExecutor.strategies.get(speedupType);
    if (!strategyClass) {
      return null;
    }
    return new strategyClass();
  }

  /**
   * Execute precheck for the given strategy
   */
  private async precheck(strategy: SpeedupStrategy): Promise<PrecheckResult> {
    try {
      logger.debug('Running precheck...');
      return await strategy.precheck(this.sandbox);
    } catch (e) {
      logger.error(`Precheck exception: ${e}`);
      return { passed: false, message: `Precheck failed with exception: ${String(e)}` };
    }
  }

}
