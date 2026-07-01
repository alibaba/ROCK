/**
 * PIP speedup strategy implementation
 *
 * Mirrors Python rock/sdk/sandbox/speedup/strategies/pip.py
 */

import { initLogger } from '../../../logger.js';
import type { AbstractSandbox } from '../../client.js';
import { SpeedupStrategy } from '../base.js';
import type { PrecheckResult } from '../base.js';
import { buildPipScript } from '../constants.js';

const logger = initLogger('rock.sandbox.speedup.strategies.pip');

/**
 * PIP speedup strategy
 *
 * Configures pip package manager to use a mirror index.
 * Requires pip to be installed in the sandbox.
 */
export class PipSpeedupStrategy extends SpeedupStrategy {
  /**
   * Check if pip is installed
   */
  async precheck(sandbox: AbstractSandbox): Promise<PrecheckResult> {
    try {
      // Try pip3 first, then pip
      const result = await sandbox.execute({
        command: ['sh', '-c', 'pip3 --version 2>&1 || pip --version 2>&1'],
        timeout: 30,
      });
      if (result.exitCode === 0) {
        const pipVersion = result.stdout.trim();
        logger.info(`PIP precheck passed: ${pipVersion}`);
        return { passed: true, message: `PIP check passed: ${pipVersion}` };
      } else {
        logger.warn('PIP precheck failed: pip not found');
        return { passed: false, message: 'pip is not installed, PIP speedup is not supported' };
      }
    } catch (e) {
      logger.error(`PIP precheck failed with exception: ${e}`);
      return { passed: false, message: `PIP check failed: ${String(e)}` };
    }
  }

  /**
   * Parse PIP mirror URL
   *
   * @param speedupValue - Mirror URL with protocol
   * @returns Parameters with pip_index_url and pip_trusted_host
   *
   * Examples:
   *   http://mirrors.cloud.aliyuncs.com -> {
   *     pip_index_url: "http://mirrors.cloud.aliyuncs.com/pypi/simple/",
   *     pip_trusted_host: "mirrors.cloud.aliyuncs.com"
   *   }
   */
  parseValue(speedupValue: string): Record<string, string> {
    // Remove trailing slash
    const baseUrl = speedupValue.replace(/\/$/, '');

    // Extract trusted host from URL
    const parsed = new URL(baseUrl);
    const trustedHost = parsed.host;

    // Build index URL by appending /pypi/simple/
    const indexUrl = `${baseUrl}/pypi/simple/`;

    return { pip_index_url: indexUrl, pip_trusted_host: trustedHost };
  }

  /**
   * Generate PIP speedup script
   */
  generateScript(speedupValue: string): string {
    const params = this.parseValue(speedupValue);
    logger.info(`Generating PIP speedup script with mirror: ${params.pip_index_url}`);
    return buildPipScript(params as { pip_index_url: string; pip_trusted_host: string });
  }
}
