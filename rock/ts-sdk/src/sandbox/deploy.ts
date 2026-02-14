/**
 * Deploy - Sandbox resource deployment manager
 */

import { existsSync, statSync } from 'fs';
import { resolve } from 'path';
import { randomUUID } from 'crypto';
import { initLogger } from '../logger.js';
import type { Sandbox } from './client.js';

const logger = initLogger('rock.sandbox.deploy');

/**
 * Deploy - Manages deployment of local directories to sandbox
 */
export class Deploy {
  private sandbox: Sandbox;
  private workingDir: string | null = null;

  constructor(sandbox: Sandbox) {
    this.sandbox = sandbox;
  }

  /**
   * Get the current working directory
   */
  getWorkingDir(): string | null {
    return this.workingDir;
  }

  /**
   * Deploy local directory to sandbox
   *
   * @param localPath - Local directory path
   * @param targetPath - Target path in sandbox (optional)
   * @returns The target path in sandbox
   */
  async deployWorkingDir(
    localPath: string,
    targetPath?: string
  ): Promise<string> {
    const localAbs = resolve(localPath);

    // Validate local path
    if (!existsSync(localAbs)) {
      throw new Error(`local_path not found: ${localAbs}`);
    }
    const stats = statSync(localAbs);
    if (!stats.isDirectory()) {
      throw new Error(`local_path must be a directory: ${localAbs}`);
    }

    // Determine target path
    const target = targetPath ?? `/tmp/rock_workdir_${randomUUID().replace(/-/g, '')}`;

    const sandboxId = this.sandbox.getSandboxId();
    logger.info(`[${sandboxId}] Deploying working_dir: ${localAbs} -> ${target}`);

    // Upload directory
    const uploadResult = await this.sandbox.getFs().uploadDir(localAbs, target);
    if (uploadResult.exit_code !== 0) {
      throw new Error(`Failed to upload directory: ${uploadResult.failure_reason}`);
    }

    // Update working directory
    this.workingDir = target;
    logger.info(`[${sandboxId}] working_dir deployed: ${target}`);

    return target;
  }

  /**
   * Format command template supporting ${} and <<>> syntax
   *
   * @param template - Template string with placeholders
   * @param kwargs - Additional substitution variables
   * @returns Formatted string
   */
  format(template: string, kwargs: Record<string, string> = {}): string {
    // Build substitution map
    const subs: Record<string, string | undefined> = {
      ...kwargs,
      ...(this.workingDir ? { working_dir: this.workingDir } : {}),
    };

    // Replace <<key>> with ${key} for template substitution
    let result = template;
    for (const key of Object.keys(subs)) {
      result = result.replace(new RegExp(`<<${key}>>`, 'g'), `\${${key}}`);
    }

    // Perform substitution
    for (const [key, value] of Object.entries(subs)) {
      if (value !== undefined) {
        result = result.replace(new RegExp(`\\$\\{${key}\\}`, 'g'), value);
      }
    }

    return result;
  }
}
