/**
 * Process - Process management for sandbox execution
 */

import { initLogger } from '../logger.js';
import type { Observation } from '../types/responses.js';
import type { Sandbox } from './client.js';

const logger = initLogger('rock.sandbox.process');

/**
 * Process management for sandbox execution
 */
export class Process {
  private sandbox: Sandbox;

  constructor(sandbox: Sandbox) {
    this.sandbox = sandbox;
  }

  /**
   * Execute a script in the sandbox
   *
   * @param scriptContent - The script content to execute
   * @param scriptName - Optional custom script name
   * @param waitTimeout - Maximum time to wait for script completion
   * @param waitInterval - Interval between process checks
   * @param cleanup - Whether to delete the script file after execution
   * @returns Observation with execution result
   */
  async executeScript(options: {
    scriptContent: string;
    scriptName?: string;
    waitTimeout?: number;
    waitInterval?: number;
    cleanup?: boolean;
  }): Promise<Observation> {
    const {
      scriptContent,
      scriptName,
      waitTimeout = 300,
      cleanup = true,
    } = options;

    const sandboxId = this.sandbox.getSandboxId();
    const timestamp = Date.now();
    const name = scriptName ?? `script_${timestamp}.sh`;
    const scriptPath = `/tmp/${name}`;

    try {
      // Upload script
      logger.info(`[${sandboxId}] Uploading script to ${scriptPath}`);
      const writeResult = await this.sandbox.write_file({
        content: scriptContent,
        path: scriptPath,
      });

      if (!writeResult.success) {
        const errorMsg = `Failed to upload script: ${writeResult.message}`;
        logger.error(errorMsg);
        return { output: errorMsg, exitCode: 1, failureReason: 'Script upload failed', expectString: '' };
      }

      // Execute script
      logger.info(`[${sandboxId}] Executing script: ${scriptPath} (timeout=${waitTimeout}s)`);
      const result = await this.sandbox.arun(`bash ${scriptPath}`, {
        mode: 'nohup',
        waitTimeout,
      });

      return result;
    } catch (e) {
      const errorMsg = `Script execution failed: ${e}`;
      logger.error(errorMsg);
      return { output: errorMsg, exitCode: 1, failureReason: errorMsg, expectString: '' };
    } finally {
      // Cleanup script if requested
      if (cleanup) {
        try {
          logger.info(`[${sandboxId}] Cleaning up script: ${scriptPath}`);
          await this.sandbox.execute({ command: ['rm', '-f', scriptPath], timeout: 30 });
        } catch (e) {
          logger.warn(`Failed to cleanup script ${scriptPath}: ${e}`);
        }
      }
    }
  }
}
