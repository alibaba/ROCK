/**
 * GitHub speedup strategy implementation
 *
 * Mirrors Python rock/sdk/sandbox/speedup/strategies/github.py
 */

import { initLogger } from '../../../logger.js';
import type { AbstractSandbox } from '../../client.js';
import { SpeedupStrategy } from '../base.js';
import type { PrecheckResult } from '../base.js';
import { buildGithubScript } from '../constants.js';

const logger = initLogger('rock.sandbox.speedup.strategies.github');

/**
 * GitHub speedup strategy for github.com acceleration
 *
 * Adds a hosts file entry to accelerate access to github.com.
 * Requires root privileges (writable /etc/hosts).
 */
export class GithubSpeedupStrategy extends SpeedupStrategy {
  /**
   * Check if /etc/hosts is writable
   */
  async precheck(sandbox: AbstractSandbox): Promise<PrecheckResult> {
    try {
      const result = await sandbox.execute({ command: ['test', '-w', '/etc/hosts'], timeout: 30 });
      if (result.exitCode === 0) {
        logger.info('GitHub precheck passed: /etc/hosts is writable');
        return { passed: true, message: 'System check passed: /etc/hosts is writable' };
      } else {
        logger.warn('GitHub precheck failed: /etc/hosts is not writable');
        return {
          passed: false,
          message: '/etc/hosts is not writable, GitHub speedup requires root privileges',
        };
      }
    } catch (e) {
      logger.error(`GitHub precheck failed with exception: ${e}`);
      return { passed: false, message: `System check failed: ${String(e)}` };
    }
  }

  /**
   * Parse GitHub IP address for github.com acceleration
   *
   * @param speedupValue - IP address for github.com
   * @returns Parameters with hosts_entry
   *
   * Examples:
   *   "11.11.11.11" -> { hosts_entry: "11.11.11.11 github.com" }
   */
  parseValue(speedupValue: string): Record<string, string> {
    // Trim whitespace
    const ipAddress = speedupValue.trim();

    // Validate IP address format
    const ipPattern = /^(\d{1,3}\.){3}\d{1,3}$/;
    if (!ipPattern.test(ipAddress)) {
      logger.warn(`Invalid IP address format: ${ipAddress}`);
      throw new Error(`Invalid IP address format: ${ipAddress}. Expected format: x.x.x.x`);
    }

    // Validate IP address range (0-255 for each octet)
    const octets = ipAddress.split('.');
    for (const octet of octets) {
      if (parseInt(octet, 10) > 255) {
        logger.warn(`Invalid IP address: ${ipAddress}, octet value exceeds 255`);
        throw new Error(`Invalid IP address: ${ipAddress}, octet value must be 0-255`);
      }
    }

    // Build hosts entry for github.com
    const hostsEntry = `${ipAddress} github.com`;
    return { hosts_entry: hostsEntry };
  }

  /**
   * Generate GitHub hosts speedup script
   */
  generateScript(speedupValue: string): string {
    const params = this.parseValue(speedupValue);
    logger.info(`Generating GitHub speedup script with hosts entry: ${params.hosts_entry}`);
    return buildGithubScript(params as { hosts_entry: string });
  }
}
