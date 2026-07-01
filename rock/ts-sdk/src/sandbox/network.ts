/**
 * Network - Network management for sandbox
 */

import { initLogger } from '../logger.js';
import type { Observation } from '../types/responses.js';
import type { Sandbox } from './client.js';
import { SpeedupExecutor } from './speedup/executor.js';
import { SpeedupType } from './speedup/types.js';

const logger = initLogger('rock.sandbox.network');

// Re-export SpeedupType for backward compatibility
export { SpeedupType };

/**
 * Network management for sandbox
 */
export class Network {
  private sandbox: Sandbox;
  private executor: SpeedupExecutor;

  constructor(sandbox: Sandbox) {
    this.sandbox = sandbox;
    this.executor = new SpeedupExecutor(sandbox);
  }

  /**
   * Configure acceleration for package managers or network resources
   *
   * @param speedupType - Type of speedup configuration
   * @param speedupValue - Speedup value (mirror URL or IP address)
   * @param timeout - Execution timeout in seconds
   * @returns Observation with execution result
   */
  async speedup(
    speedupType: SpeedupType,
    speedupValue: string,
    timeout: number = 300
  ): Promise<Observation> {
    return this.executor.execute(speedupType, speedupValue, timeout);
  }
}
