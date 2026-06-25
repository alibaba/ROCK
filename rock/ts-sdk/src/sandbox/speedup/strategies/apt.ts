/**
 * APT speedup strategy implementation
 *
 * Mirrors Python rock/sdk/sandbox/speedup/strategies/apt.py
 */

import { initLogger } from '../../../logger.js';
import type { AbstractSandbox } from '../../client.js';
import { SpeedupStrategy } from '../base.js';
import type { PrecheckResult } from '../base.js';
import { buildAptScript } from '../constants.js';

const logger = initLogger('rock.sandbox.speedup.strategies.apt');

/**
 * APT speedup strategy
 *
 * Configures APT package manager to use a mirror source.
 * Only supported on Debian/Ubuntu-based systems.
 */
export class AptSpeedupStrategy extends SpeedupStrategy {
  /**
   * Check if the system is Debian/Ubuntu based
   */
  async precheck(sandbox: AbstractSandbox): Promise<PrecheckResult> {
    try {
      const result = await sandbox.execute({ command: ['test', '-f', '/etc/debian_version'], timeout: 30 });
      if (result.exitCode === 0) {
        logger.info('APT precheck passed: Debian/Ubuntu system detected');
        return { passed: true, message: 'System check passed: Debian/Ubuntu detected' };
      } else {
        logger.warn('APT precheck failed: Not a Debian/Ubuntu system');
        return { passed: false, message: 'This is not a Debian/Ubuntu system, APT speedup is not supported' };
      }
    } catch (e) {
      logger.error(`APT precheck failed with exception: ${e}`);
      return { passed: false, message: `System check failed: ${String(e)}` };
    }
  }

  /**
   * Parse APT mirror URL
   *
   * @param speedupValue - Mirror URL with protocol
   * @returns Parameters with mirror_base
   *
   * Examples:
   *   http://mirrors.cloud.aliyuncs.com -> { mirror_base: "http://mirrors.cloud.aliyuncs.com" }
   *   https://mirrors.aliyun.com/      -> { mirror_base: "https://mirrors.aliyun.com" }
   */
  parseValue(speedupValue: string): Record<string, string> {
    // Remove trailing slash for consistency
    const mirrorBase = speedupValue.replace(/\/$/, '');
    return { mirror_base: mirrorBase };
  }

  /**
   * Generate APT speedup script
   */
  generateScript(speedupValue: string): string {
    const params = this.parseValue(speedupValue);
    logger.info(`Generating APT speedup script with mirror: ${params.mirror_base}`);
    return buildAptScript(params as { mirror_base: string });
  }
}
