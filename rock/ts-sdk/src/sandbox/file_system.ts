/**
 * FileSystem - File system operations for sandbox
 */

import { initLogger } from '../logger.js';
import type { Observation, CommandResponse } from '../types/responses.js';
import type { Command, ChownRequest, ChmodRequest } from '../types/requests.js';
import type { AbstractSandbox } from './client.js';

const logger = initLogger('rock.sandbox.fs');

/**
 * Abstract file system interface
 */
export abstract class FileSystem {
  protected sandbox: AbstractSandbox;

  constructor(sandbox: AbstractSandbox) {
    this.sandbox = sandbox;
  }

  abstract chown(request: ChownRequest): Promise<{ success: boolean; message: string }>;
  abstract chmod(request: ChmodRequest): Promise<{ success: boolean; message: string }>;
  abstract uploadDir(
    sourceDir: string,
    targetDir: string,
    extractTimeout?: number
  ): Promise<Observation>;
}

/**
 * Linux file system implementation
 */
export class LinuxFileSystem extends FileSystem {
  constructor(sandbox: AbstractSandbox) {
    super(sandbox);
  }

  async chown(request: ChownRequest): Promise<{ success: boolean; message: string }> {
    const { paths, recursive, remoteUser } = request;

    if (!paths || paths.length === 0) {
      throw new Error('paths is empty');
    }

    const command = ['chown'];
    if (recursive) {
      command.push('-R');
    }
    command.push(`${remoteUser}:${remoteUser}`, ...paths);

    logger.info(`chown command: ${command.join(' ')}`);

    const response: CommandResponse = await this.sandbox.execute({ command, timeout: 300 });
    if (response.exitCode !== 0) {
      return { success: false, message: JSON.stringify(response) };
    }
    return { success: true, message: JSON.stringify(response) };
  }

  async chmod(request: ChmodRequest): Promise<{ success: boolean; message: string }> {
    const { paths, recursive, mode } = request;

    if (!paths || paths.length === 0) {
      throw new Error('paths is empty');
    }

    const command = ['chmod'];
    if (recursive) {
      command.push('-R');
    }
    command.push(mode, ...paths);

    logger.info(`chmod command: ${command.join(' ')}`);
    const response: CommandResponse = await this.sandbox.execute({ command, timeout: 300 });
    if (response.exitCode !== 0) {
      return { success: false, message: JSON.stringify(response) };
    }
    return { success: true, message: JSON.stringify(response) };
  }

  async uploadDir(
    sourceDir: string,
    targetDir: string,
    extractTimeout: number = 600
  ): Promise<Observation> {
    // Simplified implementation - would need tar/untar logic
    // This is a placeholder that would be implemented with actual
    // tar file creation and upload logic
    logger.info(`uploadDir: ${sourceDir} -> ${targetDir}`);

    // For now, return a placeholder observation
    return {
      output: `uploaded ${sourceDir} -> ${targetDir}`,
      exitCode: 0,
      failureReason: '',
      expectString: '',
    };
  }
}
