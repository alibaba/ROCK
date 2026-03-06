/**
 * FileSystem - File system operations for sandbox
 */

import { existsSync, statSync, mkdtempSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join, resolve, basename } from 'path';
import { initLogger } from '../logger.js';
import type { Observation, CommandResponse } from '../types/responses.js';
import type { ChownRequest, ChmodRequest } from '../types/requests.js';
import type { AbstractSandbox } from './client.js';
import { RunMode } from '../common/constants.js';
import { validatePath, validateUsername, validateChmodMode, shellQuote } from '../utils/shell.js';

const logger = initLogger('rock.sandbox.fs');

/**
 * Create a tar.gz archive of a directory (simplified implementation)
 * Uses shell command `tar` for reliability
 */
async function createTarGz(sourceDir: string, outputPath: string): Promise<void> {
  const { spawn } = await import('child_process');
  
  return new Promise((resolve, reject) => {
    const tar = spawn('tar', ['-czf', outputPath, '-C', sourceDir, '.']);
    
    let stderr = '';
    tar.stderr.on('data', (data) => {
      stderr += data.toString();
    });
    
    tar.on('close', (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`tar command failed with code ${code}: ${stderr}`));
      }
    });
    
    tar.on('error', (err) => {
      reject(err);
    });
  });
}

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

    // Validate username to prevent injection
    validateUsername(remoteUser);

    // Validate all paths
    for (const p of paths) {
      validatePath(p);
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

    // Validate mode to prevent injection
    validateChmodMode(mode);

    // Validate all paths
    for (const p of paths) {
      validatePath(p);
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
    let localTarPath: string | null = null;
    let remoteTarPath: string | null = null;
    let session: string | null = null;

    try {
      // Validate source directory
      const src = resolve(sourceDir);
      if (!existsSync(src)) {
        return {
          output: '',
          exitCode: 1,
          failureReason: `source_dir not found: ${src}`,
          expectString: '',
        };
      }
      const stats = statSync(src);
      if (!stats.isDirectory()) {
        return {
          output: '',
          exitCode: 1,
          failureReason: `source_dir must be a directory: ${src}`,
          expectString: '',
        };
      }

      // Validate target directory for security
      try {
        validatePath(targetDir);
      } catch (e) {
        return {
          output: '',
          exitCode: 1,
          failureReason: e instanceof Error ? e.message : 'Invalid target directory',
          expectString: '',
        };
      }

      // Generate unique names using timestamp
      const ts = Date.now().toString();
      const tmpDir = mkdtempSync(join(tmpdir(), 'rock-upload-'));
      localTarPath = join(tmpDir, `rock_upload_${ts}.tar.gz`);
      remoteTarPath = `/tmp/rock_upload_${ts}.tar.gz`;
      session = `bash-${ts}`;

      logger.info(`uploadDir: ${src} -> ${targetDir}`);

      // Create bash session
      await this.sandbox.createSession({ session, startupSource: [], envEnable: false });

      // Check tar exists in sandbox
      const checkResult = await this.sandbox.arun('command -v tar >/dev/null 2>&1', {
        session,
        mode: RunMode.NORMAL,
      });
      if (checkResult.exitCode !== 0) {
        return {
          output: '',
          exitCode: 1,
          failureReason: 'sandbox has no tar command; cannot extract tarball',
          expectString: '',
        };
      }

      // Pack locally
      try {
        await createTarGz(src, localTarPath);
      } catch (e) {
        throw new Error(`tar pack failed: ${e}`);
      }

      // Upload tarball
      const uploadResponse = await this.sandbox.upload({
        sourcePath: localTarPath,
        targetPath: remoteTarPath,
      });
      if (!uploadResponse.success) {
        return {
          output: '',
          exitCode: 1,
          failureReason: `tar upload failed: ${uploadResponse.message}`,
          expectString: '',
        };
      }

      // Extract in sandbox using properly escaped paths
      const escapedTargetDir = shellQuote(targetDir);
      const escapedRemoteTar = shellQuote(remoteTarPath);
      const extractCmd = `rm -rf ${escapedTargetDir} && mkdir -p ${escapedTargetDir} && tar -xzf ${escapedRemoteTar} -C ${escapedTargetDir}`;
      const extractResult = await this.sandbox.arun(`bash -c ${shellQuote(extractCmd)}`, {
        session,
        mode: RunMode.NOHUP,
        waitTimeout: extractTimeout,
      });

      if (extractResult.exitCode !== 0) {
        return {
          output: '',
          exitCode: 1,
          failureReason: `tar extract failed: ${extractResult.output}`,
          expectString: '',
        };
      }

      // Cleanup remote tarball
      try {
        await this.sandbox.execute({ command: ['rm', '-f', remoteTarPath], timeout: 30 });
      } catch {
        // Ignore cleanup errors
      }

      return {
        output: `uploaded ${src} -> ${targetDir} via tar`,
        exitCode: 0,
        failureReason: '',
        expectString: '',
      };
    } catch (e) {
      return {
        output: '',
        exitCode: 1,
        failureReason: `upload_dir unexpected error: ${e}`,
        expectString: '',
      };
    } finally {
      // Cleanup local tarball and temp directory
      try {
        if (localTarPath) {
          const tmpDir = basename(localTarPath).replace(/\.tar\.gz$/, '');
          rmSync(join(tmpdir(), `rock-upload-${tmpDir.split('_').pop()}`), { recursive: true, force: true });
        }
      } catch {
        // Ignore cleanup errors
      }
    }
  }
}
