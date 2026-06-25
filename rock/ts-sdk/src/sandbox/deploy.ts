/**
 * Deploy - Sandbox resource deployment manager
 *
 * Provides:
 * - deployWorkingDir(): Deploy local directory to sandbox
 * - format(): Replace ${key} and <<key>> template placeholders
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
  private _workingDir: string | null = null;

  constructor(sandbox: Sandbox) {
    this.sandbox = sandbox;
  }

  /**
   * Returns the working_dir path deployed in the sandbox.
   */
  get workingDir(): string | null {
    return this._workingDir;
  }

  /**
   * Get the current working directory (camelCase alias)
   */
  getWorkingDir(): string | null {
    return this._workingDir;
  }

  /**
   * Deploy local directory to sandbox.
   *
   * Supports multiple calls; later calls will overwrite previous paths.
   *
   * @param localPath - Local directory path (relative or absolute)
   * @param targetPath - Target path in sandbox (default: /tmp/rock_workdir_<uuid>)
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
    if (uploadResult.exitCode !== 0) {
      throw new Error(`Failed to upload directory: ${uploadResult.failureReason}`);
    }

    // Update working directory
    this._workingDir = target;
    logger.info(`[${sandboxId}] working_dir deployed: ${target}`);

    return target;
  }

  /**
   * Format command template supporting ${} and <<>> syntax.
   *
   * Only <<key>> where key is a known variable will be replaced.
   * Other occurrences of << >> are left untouched.
   *
   * Example:
   *   deploy.format("cat <<working_dir>>/file")
   *   => "cat /tmp/rock_workdir_abc123/file"
   *
   *   deploy.format("echo $((3 << 2 >> 1))")  // unaffected
   *   => "echo $((3 << 2 >> 1))"
   *
   * @param template - Template string with placeholders
   * @param kwargs - Additional substitution variables (e.g., prompt, bin_dir)
   * @returns Formatted string
   */
  format(template: string, kwargs: Record<string, string> = {}): string {
    // Build substitution map (matching Python: includes working_dir when set, filters undefined)
    const subs: Record<string, string> = {
      ...kwargs,
    };
    if (this._workingDir) {
      subs['working_dir'] = this._workingDir;
    }

    // Filter out undefined values
    const filteredSubs: Record<string, string> = {};
    for (const [k, v] of Object.entries(subs)) {
      if (v !== undefined && v !== null) {
        filteredSubs[k] = v;
      }
    }

    // Step 1: Replace <<key>> with ${key} for known keys only
    let result = template;
    for (const key of Object.keys(filteredSubs)) {
      result = result.replace(new RegExp(`<<${key}>>`, 'g'), `\$\{${key}\}`);
    }

    // Step 2: Perform ${key} substitution
    for (const [key, value] of Object.entries(filteredSubs)) {
      result = result.replace(new RegExp(`\\$\\{${key}\\}`, 'g'), value);
    }

    return result;
  }
}